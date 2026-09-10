"""Publish a generic analysis record for the run: ``analysis:sessionAnalysisData``.

The record is the searchable, reviewable XNAT object a catalog card leaves behind. It holds
**type, status, QC and provenance** as fields, and it carries **the entire output of the run**
as resources. Since 0.6.0 (plan D20, James: "roles are views onto the output tree, not a
partition of it") the resources are fixed: ``DERIVED`` is the tool's complete output tree,
byte-for-byte and path-for-path at the resource root; ``REPORT``, ``PROVENANCE`` and ``LOGS``
hold only what the wrapup itself generated. Every other role a card names (``METRICS``, or
anything else) is a **view**: the files its globs matched, recorded as paths inside ``DERIVED``
in ``wrapup.json`` and in the record's ``results_json``, never a second copy. A reviewer QCs
the run from the record alone. No field ever holds a measurement. Design and rationale:
``docs/ROLES-AS-VIEWS.md`` here and ``development/xnat_genericProcessing_plugin/docs/``.

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
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from . import __version__
from .execution import is_reserved
from .register import LABEL_MAX, XnatContext, auth_headers, collection_label, fetch_target_label

logger = logging.getLogger(__name__)

XSI_TYPE = "analysis:sessionAnalysisData"
SUBJECT_XSI_TYPE = "analysis:subjectAnalysisData"
#: The schema caps every ``*_json`` element at 65,536 characters (analysis.xsd); a record that
#: overruns is refused outright (fmriprep full run and hippunfold on demo02, 2026-09-09).
RESULTS_JSON_MAX = 65536


def xsi_type_for(context: XnatContext) -> str:
    """The record type for the run's scope: a session assessor, or a subject assessor."""
    return SUBJECT_XSI_TYPE if context.scope == "subject" else XSI_TYPE


def record_urls(context: XnatContext, name: str, by_id: bool = False) -> tuple[str, str]:
    """``(object_url, files_base)`` for a record named by label (before create) or, with
    ``by_id``, by accession id (after).

    Session scope: the record is an image assessor of the session and its role resources live
    under ``out``. Subject scope: the record is a subject assessor, an experiment of its own,
    reached by label under the subject and by id under ``/data/experiments``, and its role
    resources are plain experiment resources (no ``out``). Whether ``name`` is an id is stated
    by the caller, not inferred from its prefix: a label may legitimately start with the site's
    accession prefix, and the prefix itself is a site setting."""
    quoted = urllib.parse.quote(name, safe="")
    if context.scope == "subject":
        if by_id:
            url = f"{context.host}/data/experiments/{quoted}"
        else:
            url = (f"{context.host}/data/projects/{urllib.parse.quote(context.project, safe='')}/subjects/"
                   f"{urllib.parse.quote(context.subject, safe='')}/experiments/{quoted}")
        return url, f"{url}/resources"
    session = urllib.parse.quote(context.session, safe="")
    url = f"{context.host}/data/experiments/{session}/assessors/{quoted}"
    return url, f"{url}/out/resources"


def created_record_id(response_text: str) -> str:
    """The accession id XNAT answers a create with (one bare token), or ``""`` when the body
    is empty or not a token, so the caller keeps addressing the record by label."""
    token = response_text.strip()
    return token if token and re.fullmatch(r"[A-Za-z0-9_.-]+", token) else ""


def bounded_results_json(summary: dict) -> str:
    """``results_json`` under the schema cap. The per-role file lists are counts (the files
    are enumerable on the record's resources); if the view lists alone still overrun, they
    become counts too and ``truncated`` names what was dropped, so a consumer knows to read
    ``PROVENANCE/wrapup.json`` for the full mapping."""
    text = json.dumps(summary)
    if len(text) <= RESULTS_JSON_MAX:
        return text
    reduced = dict(summary)
    reduced["views"] = {role: len(paths) for role, paths in (summary.get("views") or {}).items()}
    reduced["truncated"] = ["views"]
    logger.warning("results_json would be %d characters (cap %d); the view file lists are replaced by counts, "
                   "the full mapping stays in PROVENANCE/wrapup.json", len(text), RESULTS_JSON_MAX)
    return json.dumps(reduced)

ANALYSIS_NS = "http://xnatworks.io/analysis"
XNAT_NS = "http://nrg.wustl.edu/xnat"

#: seg-wrapup's roles when the card does not say otherwise. ``REPORT`` and ``PROVENANCE`` name
#: the wrapup's own artefacts (the only files those resources ever hold); ``METRICS`` is a view
#: onto ``DERIVED``: the measurement files the wrapup wrote beside the masks, listed by path.
DEFAULT_RESOURCES: dict[str, list[str]] = {
    "METRICS": ["volumes.json", "volumes.csv", "segmentation.tsv"],
    "REPORT": ["report.html"],
    "PROVENANCE": ["wrapup.json", "labels.txt", "labels.ctbl"],
}

#: The role for the data output itself (design §6; plan D16: the derivatives dataset). It holds
#: every file of the tool's output tree, unchanged, at the resource root; a wrapup that keeps the
#: tree in a subdirectory of its output (proc-wrapup: ``raw/``) names it as ``derived_root`` and
#: the prefix is dropped on the record. The tree goes whole, dotfiles included (``.bidsignore``,
#: ``.heudiconv/``); the one entry left out is the DICOM copy the card reserved at the root
#: (``.source_dicom``, :data:`segwrapup.execution.RESERVED_ROOT_NAMES`), which XNAT already holds.
DERIVED_ROLE = "DERIVED"

#: The only roles that are XNAT resources on the record. ``DERIVED`` is the tool's tree; the
#: other three hold what the wrapup generated (report, manifest/status/labels, captured logs).
#: A card's ``XNW_RESOURCE_<ROLE>`` for one of these is ignored with a warning: nothing the tool
#: wrote is ever copied out of ``DERIVED`` (a tool's HTML report links to its figures by relative
#: path, so a copy on its own is broken), and nothing the wrapup wrote ever lands in it.
FIXED_ROLES = (DERIVED_ROLE, "REPORT", "PROVENANCE", "LOGS")


@dataclass(frozen=True)
class RecordFile:
    """One file to upload: where it is on disk and its name (relative path) on the record."""

    path: Path
    name: str

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
    #: ``XNW_RESOURCE_<ROLE>`` overrides for a fixed role the card sent and the wrapup ignored
    #: (``"REPORT=report.html,raw/sub-*.html"``), so ``wrapup.json`` says what was dropped.
    ignored_overrides: tuple[str, ...] = ()

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
    def from_env(cls, environ: dict | None = None, defaults: dict[str, list[str]] | None = None) -> "RecordContract | None":
        """Parse ``XNW_CONTRACT`` (JSON) or the discrete ``XNW_*`` variables; ``None`` when neither is set.

        ``defaults`` are the role globs used when the card declares none (seg-wrapup's by default;
        proc-wrapup passes its own). The fixed roles (``DERIVED``, ``REPORT``, ``PROVENANCE``,
        ``LOGS``) always keep the wrapup's defaults: a card override for one of them is logged,
        kept in ``ignored_overrides`` and otherwise ignored (plan D20)."""
        env = os.environ if environ is None else environ
        base = dict(defaults if defaults is not None else DEFAULT_RESOURCES)
        raw = env.get("XNW_CONTRACT", "").strip()
        if not raw:
            discrete = {field: env[key].strip() for key, field in cls.DISCRETE_KEYS.items() if env.get(key, "").strip()}
            if not discrete:
                logger.info("no XNW_CONTRACT or XNW_* variables in the environment; no analysis record will be published")
                return None
            resources = dict(base)
            ignored: list[str] = []
            for key, value in env.items():
                if key.startswith("XNW_RESOURCE_") and value.strip():   # XNW_RESOURCE_METRICS="a.json,b.csv"
                    _declare(resources, ignored, _valid_role(key[len("XNW_RESOURCE_"):]),
                             [p.strip() for p in value.split(",") if p.strip()])
            return cls(resources=resources, ignored_overrides=tuple(ignored), **{k: v for k, v in discrete.items()})
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"XNW_CONTRACT is not valid JSON: {error}") from error
        if not isinstance(data, dict):
            raise ValueError("XNW_CONTRACT must be a JSON object")
        resources = dict(base)
        ignored = []
        declared = data.get("resources")
        if isinstance(declared, dict):
            for role, patterns in declared.items():
                if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                    raise ValueError(f"XNW_CONTRACT resources.{role} must be a list of file patterns")
                _declare(resources, ignored, _valid_role(str(role)), list(patterns))
        return cls(
            ignored_overrides=tuple(ignored),
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


def _reserved(path: Path, root: Path) -> bool:
    """A reserved root entry of ``root`` (the DICOM copy), or inside one: never on the record."""
    return is_reserved(path.relative_to(root))


def _valid_role(role: str) -> str:
    upper = role.strip().upper()
    if not ROLE_PATTERN.match(upper):
        raise ValueError(f"resource role {role!r} is not a valid XNAT resource label (letters, digits, underscore)")
    return upper


def _declare(resources: dict[str, list[str]], ignored: list[str], role: str, patterns: list[str]) -> None:
    """Apply one card-declared role: a view is taken as given; a fixed role keeps the default."""
    if role in FIXED_ROLES:
        logger.warning("%s is a fixed resource: the wrapup decides its files (%s); the card's globs %s are ignored "
                       "(since 0.6.0 nothing is copied out of DERIVED, and DERIVED is always the whole tool output)",
                       role, ", ".join(resources.get(role, [])) or "the tool's output tree", patterns)
        ignored.append(f"{role}={','.join(patterns)}")
        return
    resources[role] = patterns


def _tree(root: Path) -> list[Path]:
    """Every file under ``root`` (dotfiles included, reserved root entries excluded), sorted, or
    nothing when the directory is absent."""
    if not root.is_dir():
        return []
    return [p for p in sorted(root.rglob("*")) if p.is_file() and not _reserved(p, root)]


def collect_files(output_dir: Path, contract: RecordContract, derived_root: str | None = None) -> dict[str, list[RecordFile]]:
    """The record's resources: the wrapup's artefacts by fixed role, then the tool's tree as ``DERIVED``.

    ``REPORT``, ``PROVENANCE`` and ``LOGS`` take the files their (wrapup-owned) globs match at the
    output root; the first role to name a file owns it. ``DERIVED`` is the tool's output tree:
    with ``derived_root`` (proc-wrapup keeps it under ``raw/``) every file under that directory,
    named relative to it, so the dataset sits at the resource root exactly as the tool wrote it;
    without one, every file the artefact roles did not claim (seg-wrapup's masks, sidecars and
    measurements at the top level plus the tool's ``raw/`` tree). Dotfiles are files like any
    other; only the reserved root entry ``.source_dicom`` (the DICOM copy XNAT already holds) is
    never uploaded, at the output root or at the tree root. A file that is neither an artefact
    nor part of the tree (only possible with ``derived_root``) is left off the record with a
    warning: the dataset is not a place for it.
    """
    found: dict[str, list[RecordFile]] = {}
    claimed: set[Path] = set()
    tree_root = output_dir / derived_root if derived_root else None
    for role, patterns in contract.resources.items():
        if role not in FIXED_ROLES or role == DERIVED_ROLE:
            continue
        files: list[RecordFile] = []
        for pattern in patterns:
            for path in sorted(output_dir.glob(pattern)):
                if not path.is_file() or path in claimed or _reserved(path, output_dir):
                    continue
                if tree_root is not None and tree_root in path.parents:
                    continue        # a wrapup artefact glob never reaches into the tool's tree
                files.append(RecordFile(path, path.relative_to(output_dir).as_posix()))
                claimed.add(path)
        if files:
            found[role] = files
    if tree_root is not None:
        derived = [RecordFile(p, p.relative_to(tree_root).as_posix()) for p in _tree(tree_root)]
        stray = [p.relative_to(output_dir).as_posix() for p in _tree(output_dir)
                 if p not in claimed and tree_root not in p.parents]
        if stray:
            logger.warning("%d file(s) in the output are neither the tool's output (%s/) nor a wrapup artefact and "
                           "are not on the record: %s", len(stray), derived_root, ", ".join(stray))
    else:
        derived = [RecordFile(p, p.relative_to(output_dir).as_posix()) for p in _tree(output_dir) if p not in claimed]
    if derived:
        found[DERIVED_ROLE] = derived
    return found


def collect_views(output_dir: Path, contract: RecordContract, derived: list[RecordFile],
                  derived_root: str | None = None) -> dict[str, list[str]]:
    """Role -> paths inside ``DERIVED`` for every non-fixed role the card named (``METRICS``, ...).

    Globs are matched relative to the DERIVED root, so a card writes them as the dataset is laid
    out (``sub-*/**/*.json``), and only files that are on ``DERIVED`` can be named: a glob that
    reaches a wrapup artefact names nothing. A pre-0.6.0 glob that starts with the local
    ``derived_root`` (``raw/...``) is rebased with a warning so a card re-pins without breaking.
    Every declared view is present, empty when nothing matched, so a reviewer sees the miss.
    """
    root = output_dir / derived_root if derived_root else output_dir
    on_record = {f.name for f in derived}
    views: dict[str, list[str]] = {}
    for role, patterns in contract.resources.items():
        if role in FIXED_ROLES:
            continue
        names: list[str] = []
        for pattern in patterns:
            if derived_root and pattern.startswith(f"{derived_root}/"):
                logger.warning("%s glob %r is read relative to the DERIVED root since 0.6.0; drop the %s/ prefix in the card",
                               role, pattern, derived_root)
                pattern = pattern[len(derived_root) + 1:]
            for path in sorted(root.glob(pattern)):
                if not path.is_file():
                    continue
                name = path.relative_to(root).as_posix()
                if name in on_record and name not in names:      # on_record already leaves out the reserved entries
                    names.append(name)
        if not names:
            logger.warning("view %s matched no file on DERIVED (globs: %s)", role, ", ".join(patterns))
        views[role] = names
    return views


def upload_name(path: Path, output_dir: Path | None) -> str:
    """The file's name on the record: its path relative to the output directory, POSIX style."""
    if output_dir is not None:
        try:
            return path.relative_to(output_dir).as_posix()
        except ValueError:
            pass
    return path.name


def _record_files(files: dict, output_dir: Path | None) -> dict[str, list[RecordFile]]:
    """Accept plain ``Path`` lists (named relative to ``output_dir``) as well as ``RecordFile`` lists."""
    return {role: [f if isinstance(f, RecordFile) else RecordFile(f, upload_name(f, output_dir)) for f in paths]
            for role, paths in files.items()}


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
                     report: dict, results: list[dict], files: dict[str, list],
                     source_dicom_present: bool, when: datetime | None = None,
                     output_dir: Path | None = None, unmeasured_masks: int = 0,
                     facts: dict | None = None, views: dict[str, list[str]] | None = None) -> str:
    """The assessor document XNAT ingests. Fields: type, status, QC, provenance. No measurements.

    ``unmeasured_masks`` is how many delivered masks could not be measured: they still ship
    under DERIVED, so the record must not claim PASS while carrying an unusable output.
    ``facts`` lets a generic wrapup override what seg-wrapup derives from masks:
    ``run_status``, ``auto_qc``, ``container_id``, ``duration_seconds``, ``notes``, ``inputs``,
    ``config`` (execution facts: node, envelope, phases; stored as ``config_json``) and
    ``wrapup`` (the publishing wrapup's name, ``seg-wrapup`` by default). ``views`` (role ->
    paths inside DERIVED) goes into ``results_json`` so a consumer can resolve ``METRICS`` to
    files without reading ``wrapup.json``.
    """
    facts = facts or {}
    files = _record_files(files, output_dir)
    wrapup_name = facts.get("wrapup") or "seg-wrapup"
    now = when or datetime.now(timezone.utc)
    structures = sum(len(r.get("structures", [])) for r in results)
    auto_qc = facts.get("auto_qc") or ("PASS" if results and structures > 0 and unmeasured_masks == 0 else "WARN")
    inputs = facts.get("inputs") or {"scan": context.scan or report.get("scan") or "", "source_dicom": source_dicom_present,
                                     "masks": [r.get("file") for r in results], "unmeasured_masks": unmeasured_masks}
    summary = {"model": report.get("model"), "model_version": report.get("model_version"),
               "structures": structures,
               "total_volume_ml": round(sum(r.get("total_volume_ml", 0) for r in results), 2),
               "file_counts": {role: len(items) for role, items in files.items()},
               "views": views or {}}
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
        _element("wrapup_version", f"{wrapup_name} {__version__}"),
        _element("run_status", facts.get("run_status") or "SUCCEEDED"),  # a wrapup only runs after the parent succeeded, unless status.json says otherwise
        _element("container_id", facts.get("container_id")),
        _element("duration_seconds", facts.get("duration_seconds")),
        _element("publication_status", "DRAFT"),
        _element("build_timestamp", now.strftime("%Y-%m-%dT%H:%M:%S")),
        _element("supersedes_id", contract.supersedes_id),
        _element("output_resource_label", contract.output_resource_label),
        _element("output_file_count", output_count),
        _element("review_state", "PENDING_REVIEW"),
        _element("auto_qc_status", auto_qc),
        # A scan is a session fact: a subject record has no scans element even if the
        # environment carried a scan id alongside the subject.
        (f"  <analysis:scans><analysis:scan>{escape(context.scan)}</analysis:scan></analysis:scans>\n"
         if context.scan and context.scope == "session" else ""),
        _element("inputs_json", json.dumps(inputs)),
        _element("config_json", json.dumps(facts["config"]) if facts.get("config") else None),
        _element("notes", facts.get("notes") or f"Published by {wrapup_name} {__version__} from the {report.get('model')} run"),
        _element("results_json", bounded_results_json(summary)),
    ])
    if context.scope == "subject":
        # A subject assessor: owned by the subject, spanning its sessions; no scans element.
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<analysis:SubjectAnalysis xmlns:analysis="{ANALYSIS_NS}" xmlns:xnat="{XNAT_NS}" '
            f'project="{escape(context.project)}" label="{escape(label)}">\n'
            f"  <xnat:date>{now.strftime('%Y-%m-%d')}</xnat:date>\n"
            f"  <xnat:subject_ID>{escape(context.subject)}</xnat:subject_ID>\n"
            f"{body}</analysis:SubjectAnalysis>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<analysis:SessionAnalysis xmlns:analysis="{ANALYSIS_NS}" xmlns:xnat="{XNAT_NS}" '
        f'project="{escape(context.project)}" label="{escape(label)}">\n'
        f"  <xnat:date>{now.strftime('%Y-%m-%d')}</xnat:date>\n"
        f"  <xnat:imageSession_ID>{escape(context.session)}</xnat:imageSession_ID>\n"
        f"{body}</analysis:SessionAnalysis>\n"
    )


def _put(context: XnatContext, url: str, body: bytes, content_type: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, method="PUT",
                                     headers={**auth_headers(context), "Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")[:500]
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode(errors="replace")[:300]
        except (http.client.HTTPException, OSError, ValueError) as body_error:   # truncated error body
            detail = f"(error body unreadable: {body_error})"
        raise RuntimeError(f"PUT {url.split('?')[0]} failed: HTTP {error.code} {detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as error:
        # HTTPException covers http.client.InvalidURL (not a ValueError, whatever its message says)
        raise RuntimeError(f"PUT {url.split('?')[0]} failed: {error}") from error


def _request(context: XnatContext, method: str, url: str, timeout: float) -> int:
    """Status of a body-less request; HTTP errors return their code instead of raising."""
    request = urllib.request.Request(url, method=method, headers=auth_headers(context))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as error:
        # BadStatusLine/IncompleteRead/InvalidURL are HTTPExceptions; a bad XNAT_HOST can be a ValueError
        raise RuntimeError(f"{method} {url.split('?')[0]} failed: {error}") from error


def _relabel(xml: str, label: str) -> str:
    """The record document carries its label as an attribute; a retried create must match the URL.
    Both record roots are relabelled: a subject record retried under the old label would collide again."""
    return re.sub(r'(<analysis:(?:Session|Subject)Analysis[^>]*?\slabel=")[^"]*(")',
                  lambda m: m.group(1) + escape(label) + m.group(2), xml, count=1)


def publish_record(context: XnatContext, label: str, xml: str, files: dict[str, list],
                   timeout_seconds: float = 300.0, output_dir: Path | None = None) -> dict:
    """Create the record, then upload each role's files to its ``out`` resource. Raises RuntimeError.

    Create-only, enforced twice: a label that already exists on the session is refused before
    any PUT (XNAT would treat the PUT as an update of that object), and if a file upload fails
    after the create succeeded the new record is deleted again so no searchable, apparently
    complete record is left behind. File names on the record are each :class:`RecordFile`'s
    ``name`` (plain ``Path`` lists are named relative to ``output_dir``), so nested output keeps
    its shape. The outcome's ``output_paths`` lists, relative to ``output_dir``, the files
    uploaded and the empty files skipped, for a wrapup that reduces its output afterwards.
    """
    files = _record_files(files, output_dir)
    xsi_type = xsi_type_for(context)
    label_url, _ = record_urls(context, label)
    probe = _request(context, "GET", f"{label_url}?format=json", timeout_seconds)
    if probe == 200:
        raise RuntimeError(f"label {label} already exists on {context.target}; the record is create-only, "
                           "pass a fresh --record-label or let the run stamp one")
    if probe != 404:   # 401/403/5xx: cannot prove the label is free, and PUT would update if it is not
        raise RuntimeError(f"could not verify that label {label} is free (existence check answered HTTP {probe}); "
                           "not creating, because PUT to an existing label would update it")
    create_url = f"{label_url}?inbody=true"
    logger.info("publishing %s %s", xsi_type, label)
    try:
        status, text = _put(context, create_url, xml.encode(), "application/xml", timeout_seconds)
    except RuntimeError as error:
        if "HTTP 409" not in str(error):
            raise
        # Labels are unique per project: another session's run of the same pipeline claimed
        # this one in the same second (the per-session probe above cannot see it). One retry
        # with a short random suffix; a second 409 is reported.
        retry = f"{label[:LABEL_MAX - 5]}_{secrets.token_hex(2)}"
        logger.warning("label %s is taken elsewhere in the project (HTTP 409); retrying once as %s", label, retry)
        label = retry
        label_url, _ = record_urls(context, label)
        status, text = _put(context, f"{label_url}?inbody=true", _relabel(xml, retry).encode(), "application/xml", timeout_seconds)
    created_id = created_record_id(text)
    record_id = created_id or label
    record_url, files_base = record_urls(context, record_id, by_id=bool(created_id))
    uploaded: dict[str, list[str]] = {}
    skipped_empty: list[str] = []
    output_paths: dict[str, list[str]] = {"uploaded": [], "skipped_empty": []}
    try:
        for role, items in files.items():
            for item in items:
                path, name = item.path, item.name
                if path.stat().st_size == 0:
                    # XNAT answers an in-body PUT with an empty body with HTTP 500 ("request entity
                    # size is 0"); a tool that wrote nothing to stderr must not cost the record.
                    logger.warning("%s is empty; not uploaded to %s (XNAT refuses zero-byte in-body files)", name, role)
                    skipped_empty.append(name)
                    output_paths["skipped_empty"].append(upload_name(path, output_dir))
                    continue
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                url = (f"{files_base}/{role}/files/{urllib.parse.quote(name, safe='/')}"
                       f"?inbody=true&format={urllib.parse.quote(upload_format(path), safe='')}")
                _put(context, url, path.read_bytes(), content_type, timeout_seconds)
                uploaded.setdefault(role, []).append(name)
                output_paths["uploaded"].append(upload_name(path, output_dir))
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
    return {"xsi_type": xsi_type, "id": record_id, "label": label, "status": status,
            "uploaded": uploaded, "skipped_empty": skipped_empty, "url": create_url.split("?")[0],
            "output_paths": output_paths}


def publish_if_possible(args, output_dir: Path, report: dict, results: list[dict],
                        source_dicom_present: bool, unmeasured_masks: int = 0,
                        context: XnatContext | None = None, facts: dict | None = None,
                        default_resources: dict[str, list[str]] | None = None,
                        derived_root: str | None = None, manifest: dict | None = None) -> dict | None:
    """Publish when the card opted in and the context is present. Never raises.

    ``derived_root`` is the subdirectory of ``output_dir`` that holds the tool's output tree
    (proc-wrapup: ``raw``); ``DERIVED`` is that tree at the resource root. ``manifest`` is the
    wrapup's ``wrapup.json`` content: the role views and any ignored overrides are written into
    it, and to ``output_dir/wrapup.json``, before the upload, so the copy on the record's
    ``PROVENANCE`` carries the role -> path mapping a consumer resolves ``METRICS`` through.
    """
    if getattr(args, "no_publish", False):
        logger.info("analysis record skipped by flag")
        return None
    try:
        contract = RecordContract.from_env(defaults=default_resources)
    except ValueError as error:
        logger.error("analysis record not published; the contract is unusable: %s", error)
        return {"error": str(error)}
    if contract is None:
        return None
    context = context or XnatContext.from_env()
    if context is None:
        logger.error("analysis record not published; XNW_CONTRACT is set but the XNAT context is incomplete")
        return {"error": "XNAT context incomplete"}
    label = (getattr(args, "record_label", "") or "").strip() or collection_label(
        getattr(args, "model", None) or getattr(args, "pipeline", "run"), context.scan or args.scan,
        session_label=fetch_target_label(context))
    # Everything from file collection onwards is guarded: a bad contract glob (an absolute
    # pattern makes Path.glob raise NotImplementedError) must be recorded, not abort delivery
    # of the masks, report and ROI collection that are already on disk.
    try:
        files = collect_files(output_dir, contract, derived_root=derived_root)
        # Zero-byte files are dropped here, before the document is built, so output_file_count
        # and results_json never claim a file the upload would skip (Codex P2 on PR #10).
        empties = [f for items in files.values() for f in items if f.path.stat().st_size == 0]
        files = {role: [f for f in items if f.path.stat().st_size > 0] for role, items in files.items()}
        files = {role: items for role, items in files.items() if items}
        for f in empties:
            logger.warning("%s is empty; left off the record (XNAT refuses zero-byte in-body files)", f.name)
        views = collect_views(output_dir, contract, files.get(DERIVED_ROLE, []), derived_root=derived_root)
        if manifest is not None:
            manifest["views"] = views
            manifest["ignored_overrides"] = list(contract.ignored_overrides)
            (output_dir / "wrapup.json").write_text(json.dumps(manifest, indent=2))
        xml = build_record_xml(context, contract, label, report, results, files, source_dicom_present,
                               output_dir=output_dir, unmeasured_masks=unmeasured_masks, facts=facts, views=views)
        outcome = publish_record(context, label, xml, files, output_dir=output_dir)
        outcome["skipped_empty"] = sorted(set(outcome.get("skipped_empty") or []) | {f.name for f in empties})
        outcome["output_paths"]["skipped_empty"] = sorted(set(outcome["output_paths"]["skipped_empty"])
                                                          | {upload_name(f.path, output_dir) for f in empties})
        outcome["views"] = views
        outcome["ignored_overrides"] = list(contract.ignored_overrides)
        return outcome
    except (RuntimeError, ValueError, NotImplementedError, OSError) as error:
        logger.error("analysis record %s not published; files and ROI collection still delivered: %s: %s",
                     label, type(error).__name__, error, exc_info=True)
        return {"label": label, "error": f"{type(error).__name__}: {error}"}
