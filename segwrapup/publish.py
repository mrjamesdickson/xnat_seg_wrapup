"""Publish a generic analysis record for the run: ``analysis:sessionAnalysisData``.

The record is the searchable, reviewable XNAT object a catalog card leaves behind. It holds
**type, status, QC and provenance** as fields, and it carries **the entire output of the run**
as resources: ``METRICS``, ``REPORT`` and ``PROVENANCE`` for the files the contract names by
role, and ``DERIVED`` for everything else the wrapup produced (masks, label maps, viewer
sidecars). A reviewer QCs the run from the record alone. No field ever holds a measurement:
what the model measured stays in ``METRICS`` (``volumes.json``). Design and rationale:
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
import http.client
import json
import re
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

#: Files the record carries by named role when the contract does not say otherwise. Every
#: other file in the output directory goes to ``DERIVED`` (see :data:`DERIVED_ROLE`), so the
#: record's resources together are the complete run output.
DEFAULT_RESOURCES: dict[str, list[str]] = {
    "METRICS": ["volumes.json", "volumes.csv", "segmentation.tsv"],
    "REPORT": ["report.html"],
    "PROVENANCE": ["wrapup.json", "labels.txt", "labels.ctbl"],
}

#: The role for the data output itself (design §6: images, labels, transforms, meshes). Unless a
#: contract names ``DERIVED`` globs explicitly, it receives every file under the output directory,
#: recursively, that no other role claimed. Dotfiles and dot-directories are never uploaded.
DERIVED_ROLE = "DERIVED"

#: Resource roles become path segments of the upload URL, so they are validated at parse time
#: rather than escaped: an unsafe role is a contract error the wrapup records, not a request
#: that ``urllib`` rejects after the record was created.
ROLE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

#: Upload ``format`` by extension where the suffix alone would mislead XNAT (``.nii.gz`` -> ``GZ``).
FORMAT_BY_SUFFIX = {".nii.gz": "NIFTI", ".nii": "NIFTI", ".seg.dcm": "DICOM", ".dcm": "DICOM",
                    ".tsv": "TSV", ".json": "JSON", ".csv": "CSV", ".html": "HTML", ".txt": "TEXT"}


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
                    resources[_valid_role(key[len("XNW_RESOURCE_"):])] = [p.strip() for p in value.split(",") if p.strip()]
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
                resources[_valid_role(str(role))] = patterns
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


def _is_hidden(path: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def _valid_role(role: str) -> str:
    upper = role.strip().upper()
    if not ROLE_PATTERN.match(upper):
        raise ValueError(f"resource role {role!r} is not a valid XNAT resource label (letters, digits, underscore)")
    return upper


def collect_files(output_dir: Path, contract: RecordContract) -> dict[str, list[Path]]:
    """Every file in ``output_dir`` assigned to exactly one role.

    Named roles first, in contract order, no duplicates. Then ``DERIVED`` takes all remaining
    files (recursive, dotfiles excluded) unless the contract lists ``DERIVED`` globs itself, in
    which case only those are taken. The union is the whole run output minus hidden files.
    """
    found: dict[str, list[Path]] = {}
    claimed: set[Path] = set()
    explicit_derived = DERIVED_ROLE in contract.resources
    for role, patterns in contract.resources.items():
        if role == DERIVED_ROLE:
            continue
        paths: list[Path] = []
        for pattern in patterns:
            for path in sorted(output_dir.glob(pattern)):
                # first role to name a file owns it: overlapping globs (METRICS "*.json" and the
                # default PROVENANCE "wrapup.json") must not upload one file twice
                if path.is_file() and path not in paths and path not in claimed and not _is_hidden(path, output_dir):
                    paths.append(path)
        if paths:
            found[role] = paths
            claimed.update(paths)
    if explicit_derived:
        derived = [p for pattern in contract.resources[DERIVED_ROLE] for p in sorted(output_dir.glob(pattern))
                   if p.is_file() and p not in claimed and not _is_hidden(p, output_dir)]
    else:
        derived = [p for p in sorted(output_dir.rglob("*")) if p.is_file() and p not in claimed and not _is_hidden(p, output_dir)]
    if derived:
        found[DERIVED_ROLE] = list(dict.fromkeys(derived))
    return found


def upload_name(path: Path, output_dir: Path | None) -> str:
    """The file's name on the record: its path relative to the output directory, POSIX style."""
    if output_dir is not None:
        try:
            return path.relative_to(output_dir).as_posix()
        except ValueError:
            pass
    return path.name


def upload_format(path: Path) -> str:
    """XNAT ``format`` for the upload: a known name, else the suffix reduced to [A-Z0-9_], else FILE."""
    lower = path.name.lower()
    for suffix, fmt in FORMAT_BY_SUFFIX.items():
        if lower.endswith(suffix):
            return fmt
    inferred = re.sub(r"[^A-Z0-9_]", "", path.suffix.lstrip(".").upper())
    return inferred or "FILE"


def _element(name: str, value) -> str:
    if value is None or value == "":
        return ""
    return f"  <analysis:{name}>{escape(str(value))}</analysis:{name}>\n"


def build_record_xml(context: XnatContext, contract: RecordContract, label: str,
                     report: dict, results: list[dict], files: dict[str, list[Path]],
                     source_dicom_present: bool, when: datetime | None = None,
                     output_dir: Path | None = None, unmeasured_masks: int = 0) -> str:
    """The assessor document XNAT ingests. Fields: type, status, QC, provenance. No measurements.

    ``unmeasured_masks`` is how many delivered masks could not be measured: they still ship
    under DERIVED, so the record must not claim PASS while carrying an unusable output.
    """
    now = when or datetime.now(timezone.utc)
    structures = sum(len(r.get("structures", [])) for r in results)
    auto_qc = "PASS" if results and structures > 0 and unmeasured_masks == 0 else "WARN"
    inputs = {"scan": context.scan or report.get("scan") or "", "source_dicom": source_dicom_present,
              "masks": [r.get("file") for r in results], "unmeasured_masks": unmeasured_masks}
    summary = {"model": report.get("model"), "model_version": report.get("model_version"),
               "structures": structures,
               "total_volume_ml": round(sum(r.get("total_volume_ml", 0) for r in results), 2),
               "files": {role: [upload_name(p, output_dir) for p in paths] for role, paths in files.items()}}
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
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as error:
        # HTTPException covers http.client.InvalidURL (not a ValueError, whatever its message says)
        raise RuntimeError(f"PUT {url.split('?')[0]} failed: {error}") from error


def _request(context: XnatContext, method: str, url: str, timeout: float) -> int:
    """Status of a body-less request; HTTP errors return their code instead of raising."""
    credentials = base64.b64encode(f"{context.user}:{context.password}".encode()).decode()
    request = urllib.request.Request(url, method=method, headers={"Authorization": f"Basic {credentials}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"{method} {url.split('?')[0]} failed: {error}") from error


def publish_record(context: XnatContext, label: str, xml: str, files: dict[str, list[Path]],
                   timeout_seconds: float = 300.0, output_dir: Path | None = None) -> dict:
    """Create the record, then upload each role's files to its ``out`` resource. Raises RuntimeError.

    Create-only, enforced twice: a label that already exists on the session is refused before
    any PUT (XNAT would treat the PUT as an update of that object), and if a file upload fails
    after the create succeeded the new record is deleted again so no searchable, apparently
    complete record is left behind. File names on the record are paths relative to
    ``output_dir`` so nested output keeps its shape.
    """
    session = urllib.parse.quote(context.session, safe="")
    label_url = f"{context.host}/data/experiments/{session}/assessors/{urllib.parse.quote(label, safe='')}"
    probe = _request(context, "GET", f"{label_url}?format=json", timeout_seconds)
    if probe == 200:
        raise RuntimeError(f"label {label} already exists on {context.session}; the record is create-only, "
                           "pass a fresh --record-label or let the run stamp one")
    if probe != 404:   # 401/403/5xx: cannot prove the label is free, and PUT would update if it is not
        raise RuntimeError(f"could not verify that label {label} is free (existence check answered HTTP {probe}); "
                           "not creating, because PUT to an existing label would update it")
    create_url = f"{label_url}?inbody=true"
    logger.info("publishing %s %s", XSI_TYPE, label)
    status, text = _put(context, create_url, xml.encode(), "application/xml", timeout_seconds)
    record_id = text.strip() if text.strip().startswith("XNAT_") else label
    record_url = f"{context.host}/data/experiments/{session}/assessors/{urllib.parse.quote(record_id, safe='')}"
    uploaded: dict[str, list[str]] = {}
    try:
        for role, paths in files.items():
            for path in paths:
                name = upload_name(path, output_dir)
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                url = (f"{record_url}/out/resources/{role}/files/{urllib.parse.quote(name, safe='/')}"
                       f"?inbody=true&format={urllib.parse.quote(upload_format(path), safe='')}")
                _put(context, url, path.read_bytes(), content_type, timeout_seconds)
                uploaded.setdefault(role, []).append(name)
    except (RuntimeError, OSError, ValueError, http.client.HTTPException) as error:   # any post-create failure
        logger.error("upload to record %s failed after create; deleting the record so no partial record stays: %s",
                     record_id, error)
        try:
            code = _request(context, "DELETE", f"{record_url}?removeFiles=true", timeout_seconds)
            rollback = f"record {record_id} deleted (HTTP {code})" if code < 300 else f"rollback DELETE answered HTTP {code}"
        except RuntimeError as delete_error:
            logger.error("rollback of record %s failed: %s", record_id, delete_error)
            rollback = f"rollback failed: {delete_error}"
        raise RuntimeError(f"{error}; {rollback}") from error
    logger.info("analysis record %s published as %s with %d file(s)", label, record_id,
                sum(len(v) for v in uploaded.values()))
    return {"xsi_type": XSI_TYPE, "id": record_id, "label": label, "status": status,
            "uploaded": uploaded, "url": create_url.split("?")[0]}


def publish_if_possible(args, output_dir: Path, report: dict, results: list[dict],
                        source_dicom_present: bool, unmeasured_masks: int = 0) -> dict | None:
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
    # Everything from file collection onwards is guarded: a bad contract glob (an absolute
    # pattern makes Path.glob raise NotImplementedError) must be recorded, not abort delivery
    # of the masks, report and ROI collection that are already on disk.
    try:
        files = collect_files(output_dir, contract)
        xml = build_record_xml(context, contract, label, report, results, files, source_dicom_present,
                               output_dir=output_dir, unmeasured_masks=unmeasured_masks)
        return publish_record(context, label, xml, files, output_dir=output_dir)
    except (RuntimeError, ValueError, NotImplementedError, OSError) as error:
        logger.error("analysis record %s not published; files and ROI collection still delivered: %s: %s",
                     label, type(error).__name__, error, exc_info=True)
        return {"label": label, "error": f"{type(error).__name__}: {error}"}
