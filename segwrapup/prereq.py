"""record-fetch: a Container Service *setup* command that resolves a card's prerequisites.

James, 2026-09-07: "adding a prerequisite to the card for a given algorithm, then our setup
command needs to find the prereq data for execution. That way we can run these manually and
run them via a separately defined workflow." A card declares what it needs as data
(``XNW_PREREQ_<NAME>`` variables, written by the adopt tool from ``metadata.prerequisites``);
this command runs before the main container, finds the data on the session by REST, and
materialises it where the tool expects it. The tool image is untouched, the card runs the same
way by hand, in an orchestration or from an event rule, and a missing prerequisite fails the
run as ``Failed (Setup)`` with one clear line before any compute.

A setup command has one input mount (the files of the wrapper input it is attached to) and one
output mount (what the main container sees for that input). So this command copies the input
through and adds ``prereq/<name>/…`` next to it, plus ``prereq.json`` describing what it chose.

Prerequisite spec (one variable per prerequisite, ``key=value;key=value``):

- record prerequisite: ``type=<analysis_type>;pipeline=<name>;min=<version>;role=<ROLE>;accepted=true|false;id=<XNAT_E…>``
  (``id`` names one record explicitly and wins over the rules; ``accepted=true`` requires
  ``review_state ACCEPTED``; otherwise the newest SUCCEEDED record of that type/pipeline);
- resource prerequisite: ``resource=<LABEL>`` on the session (copied as ``prereq/<name>/``).

Environment: ``XNAT_HOST``/``XNAT_USER``/``XNAT_PASS`` and ``PROC_PROJECT``/``PROC_SESSION_ID``
(or the ``SEG_*`` names), all of which the Container Service gives setup containers from the
parent command.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .publish import FIXED_ROLES, RecordContract, build_record_xml, publish_record
from .register import XnatContext, auth_headers, close_session, collection_label, fetch_target_label

logger = logging.getLogger(__name__)

RECORD_TYPE = "analysis:sessionAnalysisData"
SUBJECT_RECORD_TYPE = "analysis:subjectAnalysisData"
PREFIX = "XNW_PREREQ_"
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
MANIFEST = "prereq.json"
RECORD_COLUMNS = ["ID", "label", "insert_date", "imagesession_id", "pipeline_name", "pipeline_version",
                  "analysis_type", "review_state", "run_status", "publication_status"]
#: A subject record is a subject assessor: its owner column is the subject, not a session.
SUBJECT_RECORD_COLUMNS = [c if c != "imagesession_id" else "subject_ID" for c in RECORD_COLUMNS]


@dataclass
class Prerequisite:
    name: str
    analysis_type: str = ""
    pipeline: str = ""
    min_version: str = ""
    role: str = "DERIVED"
    accepted: bool = False
    record_id: str = ""
    resource: str = ""
    scope: str = "session"          # resource: "session" or "scan"; record: "session" (default) or "subject"
    scan_type: str = ""             # scope=scan on a session-level run: the scans whose type matches this glob
    raw: str = ""

    KEYS = ("type", "pipeline", "min", "role", "accepted", "id", "resource", "scope", "scan_type")
    RECORD_SCOPES = ("session", "subject")
    RESOURCE_SCOPES = ("session", "scan")

    @classmethod
    def parse(cls, name: str, spec: str) -> "Prerequisite":
        # The name becomes the directory ``prereq/<name>/`` under the output, so it must be one
        # safe path component: no separators, no ``..``, nothing outside [a-z0-9_] (Codex P2, PR #10).
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"{PREFIX}{name}: the prerequisite name must match {NAME_RE.pattern} "
                             f"(one path component; it becomes prereq/<name>/)")
        fields: dict[str, str] = {}
        for part in spec.split(";"):
            if not part.strip():
                continue
            if "=" not in part:
                raise ValueError(f"{PREFIX}{name}: clause {part.strip()!r} is not key=value ({spec!r})")
            k, v = part.split("=", 1)
            k = k.strip().lower()
            if k in fields:
                # pipeline=trusted;pipeline=other kept only the last value; accepted=true;accepted=false
                # silently disabled the review gate (Codex P1, PR #10).
                raise ValueError(f"{PREFIX}{name}: clause {k!r} given twice ({fields[k]!r} then {v.strip()!r}) ({spec!r})")
            if k not in cls.KEYS:
                # A misspelling (pipline=) must not silently widen the selection (Codex P1, PR #10).
                raise ValueError(f"{PREFIX}{name}: unknown clause {k!r}; known: {', '.join(cls.KEYS)} ({spec!r})")
            fields[k] = v.strip()
        p = cls(name=name.lower(), analysis_type=fields.get("type", ""), pipeline=fields.get("pipeline", ""),
                min_version=fields.get("min", ""), role=(fields.get("role") or "DERIVED").upper(),
                accepted=_parse_bool(name, "accepted", fields.get("accepted", "false")),
                record_id=fields.get("id", ""), resource=fields.get("resource", ""),
                scope=(fields.get("scope") or "session").lower(), scan_type=fields.get("scan_type", ""), raw=spec)
        if not (p.resource or p.record_id or p.analysis_type or p.pipeline):
            raise ValueError(f"{PREFIX}{name}: a prerequisite needs resource=, id=, type= or pipeline= ({spec!r})")
        record_keys = [k for k in ("type", "pipeline", "min", "role", "accepted", "id") if k in fields]
        if p.resource and record_keys:
            # A resource prerequisite ignores record clauses, so mixing them would silently drop a
            # pipeline or review requirement (Codex P2, PR #10).
            raise ValueError(f"{PREFIX}{name}: resource= cannot be combined with {', '.join(k + '=' for k in record_keys)} "
                             f"(a prerequisite is either a resource or a record) ({spec!r})")
        if p.resource and p.scope not in cls.RESOURCE_SCOPES:
            raise ValueError(f"{PREFIX}{name}: scope must be session or scan for a resource prerequisite ({spec!r})")
        if not p.resource and p.scope not in cls.RECORD_SCOPES:
            # scope=subject (0.6.2): the record is a subject record (analysis:subjectAnalysisData),
            # for a consumer of a subject-level run such as xcp-d after a subject-scoped fMRIPrep.
            raise ValueError(f"{PREFIX}{name}: scope must be session or subject for a record prerequisite ({spec!r})")
        if p.scope == "scan" and not p.resource:
            raise ValueError(f"{PREFIX}{name}: scope=scan needs resource=<label on the scan> ({spec!r})")
        if not p.resource and p.scan_type:
            raise ValueError(f"{PREFIX}{name}: scan_type= applies only to resource= prerequisites ({spec!r})")
        if p.scan_type and p.scope != "scan":
            # resolve() takes the session-resource branch and never reads scan_type, so the filter
            # would be dropped and the whole session resource used instead (Codex P1, PR #10).
            raise ValueError(f"{PREFIX}{name}: scan_type= needs scope=scan ({spec!r})")
        return p

    @property
    def is_resource(self) -> bool:
        return bool(self.resource)

    def describe(self) -> str:
        """What the card asks for, in words a reviewer can act on."""
        if self.resource and self.scope == "scan":
            which = f"of scans of type '{self.scan_type}'" if self.scan_type else "of the run's scan"
            return f"the {self.resource} resource {which}"
        if self.resource:
            return f"the session resource {self.resource}"
        files = (f"files from its {self.role} resource" if self.role in FIXED_ROLES
                 else f"the DERIVED files its {self.role} view names")
        if self.record_id:
            return f"record {self.record_id} ({files})"
        what = " ".join(x for x in [f"a {self.analysis_type}" if self.analysis_type else "a",
                                     "subject record" if self.scope == "subject" else "record",
                                     f"from pipeline {self.pipeline}" if self.pipeline else "from any pipeline"] if x)
        conds = [f"version >= {self.min_version}" if self.min_version else "any version",
                 "ACCEPTED in review" if self.accepted else "review not required", "run SUCCEEDED"]
        return f"{what} ({', '.join(conds)}), {files}"


def _parse_bool(name: str, key: str, value: str) -> bool:
    """true/false only. A typo (``accepted=ture``) must not silently disable the review gate."""
    v = value.strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no", ""):
        return False
    raise ValueError(f"{PREFIX}{name}: {key}= must be true or false, not {value!r}")


def prerequisites_from_env(environ: dict | None = None) -> list[Prerequisite]:
    env = os.environ if environ is None else environ
    out: list[Prerequisite] = []
    seen: dict[str, str] = {}
    for key in sorted(env):
        if key.startswith(PREFIX) and env[key].strip():
            p = Prerequisite.parse(key[len(PREFIX):], env[key])
            if p.name in seen:
                # Two variables that differ only in case would share prereq/<name>/ (Codex P2, PR #10).
                raise ValueError(f"{key} and {seen[p.name]} both name the prerequisite {p.name!r}")
            seen[p.name] = key
            out.append(p)
    return out


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v.strip()) if x != "")


def _get_json(context: XnatContext, url: str, timeout: float):
    request = urllib.request.Request(url, headers=auth_headers(context))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def list_records(context: XnatContext, scope: str, owners: set[str], timeout: float = 60.0) -> list[dict]:
    """Every generic record of ``scope`` owned by one of ``owners`` (session ids, or subject ids
    at subject scope), newest first, as flat dicts with the record fields plus ``scope`` and
    ``owner``. The project listing is used because it carries the record columns; the
    session-scoped listing returns the session document instead of rows."""
    record_type = SUBJECT_RECORD_TYPE if scope == "subject" else RECORD_TYPE
    wanted = SUBJECT_RECORD_COLUMNS if scope == "subject" else RECORD_COLUMNS
    columns = ",".join(c if c in ("ID", "label", "insert_date") else f"{record_type}/{c}" for c in wanted)
    url = (f"{context.host}/data/projects/{urllib.parse.quote(context.project, safe='')}/experiments"
           f"?xsiType={record_type}&format=json&columns={urllib.parse.quote(columns, safe=',:/')}")
    payload = _get_json(context, url, timeout)
    rows = payload.get("ResultSet", {}).get("Result", []) if isinstance(payload, dict) else []
    key = record_type.lower() + "/"
    records = []
    for r in rows:
        flat = {"ID": r.get("ID"), "label": r.get("label"), "insert_date": r.get("insert_date")}
        flat.update({k[len(key):]: v for k, v in r.items() if k.startswith(key)})
        owner = flat.get("subject_id") if scope == "subject" else flat.get("imagesession_id")
        if owner in owners:
            flat["scope"], flat["owner"] = scope, owner
            records.append(flat)
    return sorted(records, key=lambda r: r.get("insert_date") or "", reverse=True)


def list_session_records(context: XnatContext, timeout: float = 60.0) -> list[dict]:
    """Every generic session record on the run's session, newest first."""
    return list_records(context, "session", {context.session}, timeout)


def list_subject_sessions(context: XnatContext, subject: str, timeout: float = 60.0) -> list[dict]:
    """``[{"ID", "label"}]`` of the subject's image sessions, by label. Project-scoped: the
    site-wide ``/data/subjects/<id>/experiments`` returns the subject document, not rows."""
    url = (f"{context.host}/data/projects/{urllib.parse.quote(context.project, safe='')}/subjects/"
           f"{urllib.parse.quote(subject, safe='')}/experiments?format=json&columns=ID,label,xsiType")
    payload = _get_json(context, url, timeout)
    rows = payload.get("ResultSet", {}).get("Result", []) if isinstance(payload, dict) else []
    sessions = [{"ID": r.get("ID"), "label": r.get("label") or r.get("ID")} for r in rows
                if r.get("ID") and "SessionData" in str(r.get("xsiType") or "SessionData")]
    return sorted(sessions, key=lambda s: s["label"])


def fetch_session_subject(context: XnatContext, timeout: float = 60.0) -> str:
    """The subject id of the run's session (for a session-scoped consumer that falls back to a
    subject record); empty when XNAT does not answer."""
    url = f"{context.host}/data/experiments/{urllib.parse.quote(context.session, safe='')}?format=json"
    try:
        payload = _get_json(context, url, timeout)
        return str(payload["items"][0]["data_fields"].get("subject_ID") or "").strip()
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as error:
        logger.warning("could not read the subject of session %s (%s); no subject-record fallback", context.session, error)
        return ""


def choose_record(prereq: Prerequisite, records: list[dict]) -> tuple[dict | None, str]:
    """The record that satisfies ``prereq`` or (None, reason). Explicit id wins; else the newest
    SUCCEEDED (and ACCEPTED when required) record of the type/pipeline at or above min version."""
    def seen(rs: list[dict]) -> str:
        return "; ".join(f"{r.get('ID')} ({r.get('pipeline_name')} {r.get('pipeline_version')}, {r.get('run_status')}, {r.get('review_state')})"
                         for r in rs[:5]) + (f"; and {len(rs) - 5} more" if len(rs) > 5 else "")
    if prereq.record_id:
        hit = next((r for r in records if r.get("ID") == prereq.record_id), None)
        return (hit, "") if hit else (None, f"needs {prereq.describe()}; record {prereq.record_id} is not on this session "
                                            f"(the session's records: {seen(records) or 'none'})")
    candidates = []
    for r in records:
        if prereq.analysis_type and str(r.get("analysis_type", "")).lower() != prereq.analysis_type.lower():
            continue
        if prereq.pipeline and str(r.get("pipeline_name", "")).lower() != prereq.pipeline.lower():
            continue
        candidates.append(r)
    if not candidates:
        return None, (f"needs {prereq.describe()}; this session has no such record "
                      f"(its records: {seen(records) or 'none'})")
    ok = [r for r in candidates if str(r.get("run_status", "")).upper() == "SUCCEEDED"]
    if not ok:
        return None, f"needs {prereq.describe()}; {len(candidates)} matching record(s) but none SUCCEEDED: {seen(candidates)}"
    if prereq.min_version:
        ok = [r for r in ok if _version_tuple(str(r.get("pipeline_version", "0"))) >= _version_tuple(prereq.min_version)]
        if not ok:
            return None, f"needs {prereq.describe()}; no SUCCEEDED record at version >= {prereq.min_version}: {seen(candidates)}"
    if prereq.accepted:
        accepted = [r for r in ok if str(r.get("review_state", "")).upper() == "ACCEPTED"]
        if not accepted:
            return None, (f"needs {prereq.describe()}; none of the {len(ok)} SUCCEEDED record(s) is ACCEPTED, "
                          f"review one first: {seen(ok)}")
        ok = accepted
    return ok[0], ""


def _as_record(context: XnatContext, record) -> dict:
    """A record dict from an id (a session record of the run's session) or a dict as listed."""
    if isinstance(record, dict):
        return record
    return {"ID": str(record), "scope": "session", "imagesession_id": context.session, "owner": context.session}


def _record_resource_url(context: XnatContext, record, role: str) -> str:
    record = _as_record(context, record)
    if record.get("scope") == "subject":
        # A subject record is an experiment of its own; its roles are plain experiment resources.
        return (f"{context.host}/data/experiments/{urllib.parse.quote(record['ID'], safe='')}"
                f"/resources/{urllib.parse.quote(role, safe='')}/files")
    # Assessor-scoped: ``/data/experiments/<record>/out/resources/<role>/files`` answers with the
    # record document, not a file list (demo02, 2026-09-07), and the listing then looks empty.
    owner = record.get("imagesession_id") or record.get("owner") or context.session
    return (f"{context.host}/data/experiments/{urllib.parse.quote(owner, safe='')}/assessors/"
            f"{urllib.parse.quote(record['ID'], safe='')}/out/resources/{urllib.parse.quote(role, safe='')}/files")


def download_role(context: XnatContext, record, role: str, dest: Path, timeout: float = 300.0,
                  only: set[str] | None = None) -> list[str]:
    """Copy every file of the record's resource ``role`` into ``dest`` keeping the paths.
    ``record`` is a record dict as listed, or the id of a session record of the run's session.

    ``only`` restricts the copy to those record paths (a view resolved through ``record_views``)."""
    listing = _get_json(context, f"{_record_resource_url(context, record, role)}?format=json", timeout)
    files = listing.get("ResultSet", {}).get("Result", []) if isinstance(listing, dict) else []
    written: list[str] = []
    for f in files:
        uri = f.get("URI") or ""
        rel = uri.split("/files/", 1)[1] if "/files/" in uri else (f.get("Name") or "")
        rel = urllib.parse.unquote(rel)
        if not rel or ".." in Path(rel).parts or (only is not None and rel not in only):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(f"{context.host}{uri}", headers=auth_headers(context))
        with urllib.request.urlopen(request, timeout=timeout) as response, open(target, "wb") as out:
            shutil.copyfileobj(response, out)
        written.append(rel)
    return written


def record_views(context: XnatContext, record, timeout: float = 60.0) -> dict[str, list[str]] | None:
    """The record's role -> DERIVED-path views from its ``PROVENANCE/wrapup.json``.

    ``None`` when the record predates 0.6.0 (no ``views`` in the manifest, or no manifest):
    such a record carries its views as resources of the same name instead."""
    record_id = _as_record(context, record)["ID"]
    url = f"{_record_resource_url(context, record, 'PROVENANCE')}/wrapup.json"
    try:
        manifest = _get_json(context, url, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    except ValueError as error:
        logger.warning("record %s: wrapup.json is not JSON (%s); views unknown", record_id, error)
        return None
    views = manifest.get("views") if isinstance(manifest, dict) else None
    if not isinstance(views, dict):
        return None
    return {str(role).upper(): [str(p) for p in paths] for role, paths in views.items() if isinstance(paths, list)}


def download_view(context: XnatContext, record, role: str, dest: Path, timeout: float = 300.0) -> tuple[list[str], str]:
    """Materialise a view role (``METRICS``, ...): the DERIVED files its mapping names, at their
    DERIVED paths. Returns ``(files, reason)``; a non-empty reason means the view could not be
    resolved. A record from before 0.6.0 has no views and a resource of that name instead, which
    is used as it was (a 404 there propagates as before)."""
    record_id = _as_record(context, record)["ID"]
    views = record_views(context, record, timeout=min(timeout, 60.0))
    if views is None:
        logger.info("record %s carries no views (published before 0.6.0); reading its %s resource", record_id, role)
        return download_role(context, record, role, dest, timeout), ""
    if role not in views:
        return [], (f"record {record_id} has no {role} view; its wrapup.json maps "
                    + (", ".join(sorted(views)) if views else "no view roles"))
    wanted = set(views[role])
    if not wanted:
        return [], f"record {record_id}'s {role} view names no files (its globs matched nothing in DERIVED)"
    got = download_role(context, record, "DERIVED", dest, timeout, only=wanted)
    missing = sorted(wanted - set(got))
    if missing:
        return got, (f"record {record_id}'s {role} view names {len(missing)} file(s) that are not on its DERIVED "
                     f"resource: {', '.join(missing[:5])}" + (f", and {len(missing) - 5} more" if len(missing) > 5 else ""))
    return got, ""


def download_session_resource(context: XnatContext, label: str, dest: Path, timeout: float = 300.0,
                              session: str = "") -> list[str]:
    sid = urllib.parse.quote(session or context.session, safe="")
    listing = _get_json(context, f"{context.host}/data/experiments/{sid}/resources/{urllib.parse.quote(label, safe='')}/files?format=json", timeout)
    return _download_listing(context, listing, dest, timeout)


def _download_listing(context: XnatContext, listing, dest: Path, timeout: float) -> list[str]:
    files = listing.get("ResultSet", {}).get("Result", []) if isinstance(listing, dict) else []
    written: list[str] = []
    for f in files:
        uri = f.get("URI") or ""
        rel = urllib.parse.unquote(uri.split("/files/", 1)[1] if "/files/" in uri else (f.get("Name") or ""))
        if not rel or ".." in Path(rel).parts:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(f"{context.host}{uri}", headers=auth_headers(context))
        with urllib.request.urlopen(request, timeout=timeout) as response, open(target, "wb") as out:
            shutil.copyfileobj(response, out)
        written.append(rel)
    return written


def list_scans(context: XnatContext, timeout: float = 60.0) -> list[dict]:
    """The session's scans as ``{"ID", "type", "series_description"}`` rows."""
    sid = urllib.parse.quote(context.session, safe="")
    payload = _get_json(context, f"{context.host}/data/experiments/{sid}/scans?format=json", timeout)
    rows = payload.get("ResultSet", {}).get("Result", []) if isinstance(payload, dict) else []
    return [{"ID": str(r.get("ID") or ""), "type": r.get("type") or "", "series_description": r.get("series_description") or ""} for r in rows]


def download_scan_resource(context: XnatContext, scan_id: str, label: str, dest: Path, timeout: float = 300.0) -> list[str]:
    sid = urllib.parse.quote(context.session, safe="")
    listing = _get_json(context, f"{context.host}/data/experiments/{sid}/scans/{urllib.parse.quote(scan_id, safe='')}"
                                 f"/resources/{urllib.parse.quote(label, safe='')}/files?format=json", timeout)
    return _download_listing(context, listing, dest, timeout)


@dataclass
class Resolution:
    name: str
    kind: str
    record: dict | None = None
    resource: str = ""
    role: str = ""
    scans: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    path: str = ""
    error: str = ""
    #: Subject scope, a session-scoped prerequisite: one record per session of the subject,
    #: by session label; ``record`` is then the newest of them.
    per_session: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "path": self.path, "files": len(self.files)}
        if self.scans:
            d["scans"] = list(self.scans)
        if self.per_session:
            d["sessions"] = {label: {k: r.get(k) for k in ("ID", "label", "pipeline_name", "pipeline_version", "review_state", "run_status")}
                             for label, r in self.per_session.items()}
        if self.record:
            d["record"] = {k: self.record.get(k) for k in ("ID", "label", "pipeline_name", "pipeline_version", "review_state", "run_status", "scope")}
            d["role"] = self.role
        if self.resource:
            d["resource"] = self.resource
        if self.error:
            d["error"] = self.error
        return d


def resolve(context: XnatContext, prereqs: list[Prerequisite], records: list[dict] | None = None) -> list[Resolution]:
    """Decide, without downloading, which record or resource satisfies each prerequisite.

    Scope rules (0.6.2). A session-scoped run looks at its own session's records first and,
    when none satisfies a session-scoped prerequisite, at the subject's records (a subject-level
    fMRIPrep covers each of its sessions); ``scope=subject`` looks only at the subject's records.
    A subject-scoped run needs a session-scoped prerequisite on every session of the subject
    (the setup assembled them all) and finds ``scope=subject`` ones on the subject itself.
    """
    if records is None and context.scope == "session" and any(not p.is_resource and p.scope == "session" for p in prereqs):
        records = list_session_records(context)          # only when a session-record prerequisite needs it (Codex P2, PR #10)
    records = records or []
    out: list[Resolution] = []
    scans: list[dict] | None = None
    subject_records: list[dict] | None = None
    subject_sessions: list[dict] | None = None
    session_records_by_owner: dict[str, list[dict]] = {}
    subject = context.subject
    for p in prereqs:
        if context.scope == "subject" and p.is_resource and p.scope == "scan":
            out.append(Resolution(name=p.name, kind="scan-resource", resource=p.resource,
                                  error=f"needs {p.describe()}; scan resources cannot be gathered for a subject-scoped run"))
            continue
        if context.scope == "subject" and p.is_resource:
            # The session resource of every session of the subject, each under its label.
            subject_sessions = list_subject_sessions(context, subject) if subject_sessions is None else subject_sessions
            out.append(Resolution(name=p.name, kind="resource", resource=p.resource,
                                  per_session={s["label"]: {"ID": s["ID"], "label": s["label"]} for s in subject_sessions},
                                  error="" if subject_sessions else f"needs {p.describe()}; subject {subject} has no image sessions"))
            continue
        if not p.is_resource and p.scope == "subject":
            if not subject:
                subject = fetch_session_subject(context)
            if subject_records is None:
                subject_records = list_records(context, "subject", {subject}) if subject else []
            hit, why = choose_record(p, subject_records)
            out.append(Resolution(name=p.name, kind="record", record=hit, role=p.role,
                                  error=why.replace("this session", f"subject {subject or '?'}") if why else ""))
            continue
        if not p.is_resource and context.scope == "subject":
            # Every session of the subject must satisfy it: the tree the App sees spans them all.
            subject_sessions = list_subject_sessions(context, subject) if subject_sessions is None else subject_sessions
            if not session_records_by_owner and subject_sessions:
                for r in list_records(context, "session", {s["ID"] for s in subject_sessions}):
                    session_records_by_owner.setdefault(r["owner"], []).append(r)
            per_session, unmet = {}, []
            for s in subject_sessions:
                hit, why = choose_record(p, session_records_by_owner.get(s["ID"], []))
                if hit:
                    per_session[s["label"]] = hit
                else:
                    unmet.append(f"{s['label']}: {why}")
            newest = max(per_session.values(), key=lambda r: r.get("insert_date") or "") if per_session else None
            error = ""
            if not subject_sessions:
                error = f"needs {p.describe()}; subject {subject} has no image sessions"
            elif unmet:
                error = (f"needs {p.describe()} on every session of subject {subject}; "
                         f"{len(unmet)} of {len(subject_sessions)} unmet: " + " | ".join(unmet))
            out.append(Resolution(name=p.name, kind="record", record=newest, role=p.role, per_session=per_session, error=error))
            continue
        if p.is_resource and p.scope == "scan":
            if context.scan:
                out.append(Resolution(name=p.name, kind="scan-resource", resource=p.resource, scans=[context.scan]))
            elif p.scan_type:
                scans = list_scans(context) if scans is None else scans
                hits = [s["ID"] for s in scans if fnmatch.fnmatchcase(s["type"], p.scan_type)]
                out.append(Resolution(name=p.name, kind="scan-resource", resource=p.resource, scans=hits,
                                      error="" if hits else f"needs {p.describe()}; no scan of type {p.scan_type!r} on this session "
                                                            f"(scan types present: {sorted({s['type'] for s in scans}) or 'none'})"))
            else:
                out.append(Resolution(name=p.name, kind="scan-resource", resource=p.resource,
                                      error=f"needs {p.describe()}; scope=scan needs a scan-level run (PROC_SCAN_ID) or scan_type=<glob> in the card"))
            continue
        if p.is_resource:
            out.append(Resolution(name=p.name, kind="resource", resource=p.resource))
            continue
        hit, why = choose_record(p, records)
        if hit is None and not p.record_id:
            # Own scope first, then the subject: a subject-level run of the same pipeline
            # covers this session (it holds sub-X/ses-Y for every session).
            if not subject:
                subject = fetch_session_subject(context)
            if subject_records is None:
                subject_records = list_records(context, "subject", {subject}) if subject else []
            fallback, _ = choose_record(p, subject_records)
            if fallback is not None:
                logger.info("prerequisite %s: no session record satisfies it; using subject record %s (%s %s)",
                            p.name, fallback.get("ID"), fallback.get("pipeline_name"), fallback.get("pipeline_version"))
                hit, why = fallback, ""
            elif subject_records:
                why += f"; nor do the subject's {len(subject_records)} record(s)"
        out.append(Resolution(name=p.name, kind="record", record=hit, role=p.role, error=why))
    return out


def materialise(context: XnatContext, resolutions: list[Resolution], output_dir: Path,
                prereqs: list[Prerequisite] | None = None) -> None:
    wanted = {p.name: p for p in prereqs or []}
    for r in resolutions:
        if r.error:
            continue
        needs = f"needs {wanted[r.name].describe()}; " if r.name in wanted else ""
        dest = output_dir / "prereq" / r.name
        dest.mkdir(parents=True, exist_ok=True)
        r.path = f"prereq/{r.name}"
        try:
            if r.per_session and r.kind == "resource":
                for label, s in r.per_session.items():
                    got = download_session_resource(context, r.resource, dest / label, session=s["ID"])
                    r.files.extend(f"{label}/{f}" for f in got)
                    if not got:
                        r.error = f"{needs}session {label} holds no files in its {r.resource} resource"
                continue
            if r.per_session and r.kind == "record":
                # One directory per session label, each holding that session's record role.
                for label, record in r.per_session.items():
                    if r.role in FIXED_ROLES:
                        got = download_role(context, record, r.role, dest / label)
                    else:
                        got, why = download_view(context, record, r.role, dest / label)
                        if why:
                            r.error = f"{needs}{why}"
                            break
                    r.files.extend(f"{label}/{f}" for f in got)
                    if not got:
                        r.error = (f"{needs}session {label}: chose {record.get('ID')} but its {r.role} resource holds no files")
                        break
                continue
            if r.kind == "scan-resource":
                missing = []
                for scan in r.scans:
                    got = download_scan_resource(context, scan, r.resource, dest / scan)
                    r.files.extend(f"{scan}/{f}" for f in got)
                    if not got:
                        missing.append(scan)
                if missing:
                    r.error = f"{needs}scan {', '.join(missing)} has no files in its {r.resource} resource"
                continue
            if r.kind == "resource":
                r.files = download_session_resource(context, r.resource, dest)
            elif r.role in FIXED_ROLES:
                r.files = download_role(context, r.record, r.role, dest)
            else:
                # A view role (METRICS, ...) is a mapping onto DERIVED since 0.6.0, not a resource.
                r.files, why = download_view(context, r.record, r.role, dest)
                if why:
                    r.error = f"{needs}{why}"
                    continue
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            # A declared resource or role that does not exist is an unmet prerequisite with a
            # reason, not a transport failure (Codex P2 on PR #10).
            what = (f"record {r.record.get('ID')} has no {r.role} resource" if r.record
                    else f"the {r.resource} resource does not exist" + (f" on scan(s) {', '.join(r.scans)}" if r.scans else " on this session"))
            r.error = f"{needs}{what} (XNAT answered 404)"
            continue
        if not r.files and not r.error:
            if r.record:
                r.error = (f"{needs}chose {r.record.get('ID')} ({r.record.get('pipeline_name')} {r.record.get('pipeline_version')}, "
                           f"{r.record.get('run_status')}, {r.record.get('review_state')}) but its {r.role} resource holds no files")
            else:
                r.error = f"{needs}session resource {r.resource} holds no files"


def publish_failure_record(context: XnatContext, resolutions: list["Resolution"], output_dir: Path) -> dict | None:
    """Record an unmet prerequisite as a FAILED analysis record on the session, so the reason
    is where the reviewer looks (the Processing table) instead of only in a setup log the
    Container Service reports as ``Failed (Setup)``. The record carries no output; ``notes``
    names the unmet prerequisite(s), ``inputs_json`` the full resolution, ``prereq.json`` is
    attached under PROVENANCE. ``run_status`` FAILED keeps it from ever satisfying a
    prerequisite itself. Needs the card's XNW_* contract (the same variables proc-wrapup
    publishes with); without it nothing is recorded and ``None`` is returned. Never raises."""
    try:
        contract = RecordContract.from_env()
    except ValueError as error:
        logger.error("failure not recorded as an analysis record; the contract is unusable: %s", error)
        return {"error": str(error)}
    if contract is None:
        logger.info("no XNW_* contract in the environment: the failure is not recorded as an analysis record")
        return None
    # Same PROC -> SEG -> contract chain as proc-wrapup and seg-wrapup, so a setup failure on a
    # segmentation card names the same pipeline its successful runs would (Codex P2, PR #10).
    pipeline = (os.environ.get("PROC_PIPELINE_NAME") or os.environ.get("SEG_MODEL_NAME")
                or contract.card_id or "run")
    version = (os.environ.get("PROC_PIPELINE_VERSION") or os.environ.get("SEG_MODEL_VERSION")
               or contract.card_revision or "")
    reasons = " | ".join(f"prerequisite '{r.name}' {r.error}" for r in resolutions if r.error)
    session_label = fetch_target_label(context)
    label = collection_label(pipeline, context.scan, session_label=session_label) + "_record"
    facts = {"wrapup": "record-fetch", "run_status": "FAILED", "auto_qc": "FAIL",
             "notes": (f"{pipeline} {version} did not run on {context.scope} {session_label}"
                       + (f" scan {context.scan}" if context.scan else "") + f": {reasons}. "
                       f"Nothing was computed; recorded at setup by record-fetch {__version__}."),
             "inputs": {"scan": context.scan, "stage": "setup", "prerequisites": [r.as_dict() for r in resolutions]}}
    files = {"PROVENANCE": [output_dir / MANIFEST]}
    try:
        xml = build_record_xml(context, contract, label, {"model": pipeline, "model_version": version, "scan": context.scan},
                               [], files, False, output_dir=output_dir, facts=facts)
        record = publish_record(context, label, xml, files, output_dir=output_dir)
    except (RuntimeError, ValueError, OSError) as error:
        logger.error("failure record %s not published: %s: %s", label, type(error).__name__, error, exc_info=True)
        return {"label": label, "error": f"{type(error).__name__}: {error}"}
    logger.info("failure recorded as analysis record %s (%s)", record["id"], label)
    return record


def passthrough(input_dir: Path, output_dir: Path) -> int:
    n = 0
    if input_dir.is_dir():
        for path in input_dir.rglob("*"):
            if path.is_file():
                target = output_dir / path.relative_to(input_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                n += 1
    return n


def build_parser() -> argparse.ArgumentParser:
    env = os.environ
    parser = argparse.ArgumentParser(prog="record-fetch", description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", type=Path, default=Path(env.get("SETUP_INPUT", "/input")))
    parser.add_argument("--output", type=Path, default=Path(env.get("SETUP_OUTPUT", "/output")))
    parser.add_argument("--no-passthrough", action="store_true", help="do not copy /input into /output")
    return parser


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args.output.mkdir(parents=True, exist_ok=True)
    copied = 0 if args.no_passthrough else passthrough(args.input, args.output)
    logger.info("passed %d input file(s) through", copied)
    try:
        prereqs = prerequisites_from_env()
    except ValueError as error:
        logger.error("%s", error)
        return 2
    manifest = {"prerequisites": [], "passthrough_files": copied}
    if not prereqs:
        logger.info("no %s* variables: nothing to fetch", PREFIX)
        (args.output / MANIFEST).write_text(json.dumps(manifest, indent=2))
        return 0
    context = XnatContext.from_env()
    if context is None:
        logger.error("prerequisites declared but the XNAT context is incomplete (XNAT_HOST/USER/PASS, PROC_PROJECT, PROC_SESSION_ID or PROC_SUBJECT_ID)")
        return 2
    try:
        try:
            resolutions = resolve(context, prereqs)
            materialise(context, resolutions, args.output, prereqs)
        except (urllib.error.URLError, OSError, ValueError) as error:
            logger.error("could not resolve prerequisites: %s", error)
            return 2
        manifest["prerequisites"] = [r.as_dict() for r in resolutions]
        (args.output / MANIFEST).write_text(json.dumps(manifest, indent=2))
        failed = [r for r in resolutions if r.error]
        for r in resolutions:
            if r.error:
                logger.error("prerequisite %s: %s", r.name, r.error)
            else:
                what = r.record["ID"] + " " + (r.record.get("label") or "") if r.record else "resource " + r.resource
                logger.info("prerequisite %s: %s -> %s (%d file(s))", r.name, what, r.path, len(r.files))
        if failed:
            logger.error("%d prerequisite(s) unmet; the run stops here (Failed (Setup)) before any compute", len(failed))
            manifest["analysis_record"] = publish_failure_record(context, resolutions, args.output)
            (args.output / MANIFEST).write_text(json.dumps(manifest, indent=2))
            return 3
        return 0
    finally:
        close_session(context)


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
