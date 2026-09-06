"""Publish a generic analysis record for the run: ``analysis:sessionAnalysisData``.

The record is the searchable, reviewable XNAT object a catalog card leaves behind next to
its files. It holds **type, status, QC and provenance** and points at the files; it never
holds a measurement. What the model measured stays in the ``METRICS`` resource
(``volumes.json``), exactly as before. Design and rationale:
``development/xnat_genericProcessing_plugin/docs/DATATYPE-SPEC.md``.

The wrapup already has everything the record needs: the Container Service injects
``XNAT_HOST``/``XNAT_USER``/``XNAT_PASS`` (an alias token) and the parent command passes
``SEG_PROJECT``/``SEG_SESSION_ID``/``SEG_SCAN_ID``. The card's results contract arrives as
discrete ``XNW_*`` environment variables (``XNW_CARD_ID``, ``XNW_CONTAINER_DIGEST``, ...; set
by the registry installer from the card's ``results`` block) or, where 255 characters suffice,
as one ``XNW_CONTRACT`` JSON value. **No contract, no record**: cards opt in, nothing else changes.

Publishing is create-or-fail and additive: a failure is logged and recorded in
``wrapup.json`` and the masks, report and ROI collection still ship. The record is never
updated; a rerun is a new record whose ``supersedes_id`` may point at the old one.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from . import __version__
from .register import XnatContext, collection_label

logger = logging.getLogger(__name__)

XSI_TYPE = "analysis:sessionAnalysisData"
ANALYSIS_NS = "http://xnatworks.io/analysis"
XNAT_NS = "http://nrg.wustl.edu/xnat"

#: Files the record carries, by resource role, when the contract does not say otherwise.
#: Masks, SEG and label maps stay on the parent's resource: the record points at them.
DEFAULT_RESOURCES: dict[str, list[str]] = {
    "METRICS": ["volumes.json", "volumes.csv", "segmentation.tsv"],
    "REPORT": ["report.html"],
    "PROVENANCE": ["wrapup.json", "labels.txt", "labels.ctbl"],
}


@dataclass(frozen=True)
class RecordContract:
    """The card's results block, as the registry installer serialises it into ``XNW_CONTRACT``."""

    card_id: str = ""
    card_revision: str = ""
    contract_version: str = "0.1"
    analysis_type: str = "segmentation"
    container_image: str = ""
    container_digest: str = ""
    output_resource_label: str = ""
    supersedes_id: str = ""
    resources: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_RESOURCES))

    #: Discrete environment variables, the form the Container Service can actually store: its
    #: command table caps each environment value at 255 characters, so a JSON contract does not
    #: fit once it carries an image digest. ``XNW_CONTRACT`` (JSON) is still honoured when present.
    DISCRETE_KEYS = {
        "XNW_CARD_ID": "card_id", "XNW_CARD_REVISION": "card_revision",
        "XNW_CONTRACT_VERSION": "contract_version", "XNW_ANALYSIS_TYPE": "analysis_type",
        "XNW_CONTAINER_IMAGE": "container_image", "XNW_CONTAINER_DIGEST": "container_digest",
        "XNW_OUTPUT_RESOURCE_LABEL": "output_resource_label", "XNW_SUPERSEDES_ID": "supersedes_id",
    }

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "RecordContract | None":
        """Parse ``XNW_CONTRACT`` (JSON) or the discrete ``XNW_*`` variables; ``None`` when neither is set."""
        env = os.environ if environ is None else environ
        raw = env.get("XNW_CONTRACT", "").strip()
        if not raw:
            discrete = {field: env[key].strip() for key, field in cls.DISCRETE_KEYS.items() if env.get(key, "").strip()}
            if not discrete:
                logger.info("no XNW_CONTRACT or XNW_* variables in the environment; no analysis record will be published")
                return None
            resources = dict(DEFAULT_RESOURCES)
            for key, value in env.items():
                if key.startswith("XNW_RESOURCE_") and value.strip():   # XNW_RESOURCE_METRICS="a.json,b.csv"
                    resources[key[len("XNW_RESOURCE_"):].upper()] = [p.strip() for p in value.split(",") if p.strip()]
            return cls(resources=resources, **{k: v for k, v in discrete.items()})
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"XNW_CONTRACT is not valid JSON: {error}") from error
        if not isinstance(data, dict):
            raise ValueError("XNW_CONTRACT must be a JSON object")
        resources = dict(DEFAULT_RESOURCES)
        declared = data.get("resources")
        if isinstance(declared, dict):
            for role, patterns in declared.items():
                if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                    raise ValueError(f"XNW_CONTRACT resources.{role} must be a list of file patterns")
                resources[str(role).upper()] = patterns
        return cls(
            card_id=str(data.get("card_id", "")),
            card_revision=str(data.get("card_revision", "")),
            contract_version=str(data.get("contract_version", "0.1")),
            analysis_type=str(data.get("analysis_type", "segmentation")),
            container_image=str(data.get("container_image", "")),
            container_digest=str(data.get("container_digest", "")),
            output_resource_label=str(data.get("output_resource_label", "")),
            supersedes_id=str(data.get("supersedes_id", "")),
            resources=resources,
        )


def collect_files(output_dir: Path, contract: RecordContract) -> dict[str, list[Path]]:
    """The files that exist in ``output_dir`` for each role, in contract order, no duplicates."""
    found: dict[str, list[Path]] = {}
    for role, patterns in contract.resources.items():
        paths: list[Path] = []
        for pattern in patterns:
            for path in sorted(output_dir.glob(pattern)):
                if path.is_file() and path not in paths:
                    paths.append(path)
        if paths:
            found[role] = paths
    return found


def _element(name: str, value) -> str:
    if value is None or value == "":
        return ""
    return f"  <analysis:{name}>{escape(str(value))}</analysis:{name}>\n"


def build_record_xml(context: XnatContext, contract: RecordContract, label: str,
                     report: dict, results: list[dict], files: dict[str, list[Path]],
                     source_dicom_present: bool, when: datetime | None = None) -> str:
    """The assessor document XNAT ingests. Fields: type, status, QC, provenance. No measurements."""
    now = when or datetime.now(timezone.utc)
    structures = sum(len(r.get("structures", [])) for r in results)
    auto_qc = "PASS" if results and structures > 0 else "WARN"
    inputs = {"scan": context.scan or report.get("scan") or "", "source_dicom": source_dicom_present,
              "masks": [r.get("file") for r in results]}
    summary = {"model": report.get("model"), "model_version": report.get("model_version"),
               "structures": structures,
               "total_volume_ml": round(sum(r.get("total_volume_ml", 0) for r in results), 2),
               "files": {role: [p.name for p in paths] for role, paths in files.items()}}
    output_count = sum(len(v) for v in files.values())
    body = "".join([
        _element("analysis_type", contract.analysis_type),
        _element("pipeline_name", report.get("model")),
        _element("pipeline_version", report.get("model_version")),
        _element("container_image", contract.container_image),
        _element("container_digest", contract.container_digest),
        _element("card_id", contract.card_id),
        _element("card_revision", contract.card_revision),
        _element("contract_version", contract.contract_version),
        _element("wrapup_version", f"seg-wrapup {__version__}"),
        _element("run_status", "SUCCEEDED"),  # a wrapup only runs after the parent succeeded
        _element("publication_status", "DRAFT"),
        _element("build_timestamp", now.strftime("%Y-%m-%dT%H:%M:%S")),
        _element("supersedes_id", contract.supersedes_id),
        _element("output_resource_label", contract.output_resource_label),
        _element("output_file_count", output_count),
        _element("review_state", "PENDING_REVIEW"),
        _element("auto_qc_status", auto_qc),
        (f"  <analysis:scans><analysis:scan>{escape(context.scan)}</analysis:scan></analysis:scans>\n"
         if context.scan else ""),
        _element("inputs_json", json.dumps(inputs)),
        _element("notes", f"Published by seg-wrapup {__version__} from the {report.get('model')} run"),
        _element("results_json", json.dumps(summary)),
    ])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<analysis:SessionAnalysis xmlns:analysis="{ANALYSIS_NS}" xmlns:xnat="{XNAT_NS}" '
        f'project="{escape(context.project)}" label="{escape(label)}">\n'
        f"  <xnat:date>{now.strftime('%Y-%m-%d')}</xnat:date>\n"
        f"  <xnat:imageSession_ID>{escape(context.session)}</xnat:imageSession_ID>\n"
        f"{body}</analysis:SessionAnalysis>\n"
    )


def _put(context: XnatContext, url: str, body: bytes, content_type: str, timeout: float) -> tuple[int, str]:
    credentials = base64.b64encode(f"{context.user}:{context.password}".encode()).decode()
    request = urllib.request.Request(url, data=body, method="PUT",
                                     headers={"Authorization": f"Basic {credentials}", "Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")[:500]
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"PUT {url.split('?')[0]} failed: HTTP {error.code} {error.read().decode(errors='replace')[:300]}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"PUT {url.split('?')[0]} failed: {error}") from error


def publish_record(context: XnatContext, label: str, xml: str, files: dict[str, list[Path]],
                   timeout_seconds: float = 300.0) -> dict:
    """Create the record, then upload each role's files to its ``out`` resource. Raises RuntimeError."""
    session = urllib.parse.quote(context.session, safe="")
    create_url = f"{context.host}/data/experiments/{session}/assessors/{urllib.parse.quote(label, safe='')}?inbody=true"
    logger.info("publishing %s %s", XSI_TYPE, label)
    status, text = _put(context, create_url, xml.encode(), "application/xml", timeout_seconds)
    record_id = text.strip() if text.strip().startswith("XNAT_") else label
    uploaded: dict[str, list[str]] = {}
    for role, paths in files.items():
        for path in paths:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            fmt = path.suffix.lstrip(".").upper() or "FILE"
            url = (f"{context.host}/data/experiments/{session}/assessors/{urllib.parse.quote(record_id, safe='')}"
                   f"/out/resources/{role}/files/{urllib.parse.quote(path.name, safe='')}?inbody=true&format={fmt}")
            _put(context, url, path.read_bytes(), content_type, timeout_seconds)
            uploaded.setdefault(role, []).append(path.name)
    logger.info("analysis record %s published as %s with %d file(s)", label, record_id,
                sum(len(v) for v in uploaded.values()))
    return {"xsi_type": XSI_TYPE, "id": record_id, "label": label, "status": status,
            "uploaded": uploaded, "url": create_url.split("?")[0]}


def publish_if_possible(args, output_dir: Path, report: dict, results: list[dict],
                        source_dicom_present: bool) -> dict | None:
    """Publish when the card opted in and the context is present. Never raises."""
    if getattr(args, "no_publish", False):
        logger.info("analysis record skipped by flag")
        return None
    try:
        contract = RecordContract.from_env()
    except ValueError as error:
        logger.error("analysis record not published; the contract is unusable: %s", error)
        return {"error": str(error)}
    if contract is None:
        return None
    context = XnatContext.from_env()
    if context is None:
        logger.error("analysis record not published; XNW_CONTRACT is set but the XNAT context is incomplete")
        return {"error": "XNAT context incomplete"}
    label = (getattr(args, "record_label", "") or "").strip() or collection_label(args.model, context.scan or args.scan)
    files = collect_files(output_dir, contract)
    xml = build_record_xml(context, contract, label, report, results, files, source_dicom_present)
    try:
        return publish_record(context, label, xml, files)
    except RuntimeError as error:
        logger.error("analysis record not published; files and ROI collection still delivered: %s", error)
        return {"label": label, "error": str(error)}
