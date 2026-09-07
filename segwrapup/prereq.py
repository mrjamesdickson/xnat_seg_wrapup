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
from .publish import RecordContract, build_record_xml, publish_record
from .register import XnatContext, auth_headers, close_session, collection_label, fetch_session_label

logger = logging.getLogger(__name__)

RECORD_TYPE = "analysis:sessionAnalysisData"
PREFIX = "XNW_PREREQ_"
MANIFEST = "prereq.json"
RECORD_COLUMNS = ["ID", "label", "insert_date", "imagesession_id", "pipeline_name", "pipeline_version",
                  "analysis_type", "review_state", "run_status", "publication_status"]


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
    scope: str = "session"          # "session" (a session resource) or "scan" (a resource on a scan)
    scan_type: str = ""             # scope=scan on a session-level run: the scans whose type matches this glob
    raw: str = ""

    @classmethod
    def parse(cls, name: str, spec: str) -> "Prerequisite":
        fields: dict[str, str] = {}
        for part in spec.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                fields[k.strip().lower()] = v.strip()
        p = cls(name=name.lower(), analysis_type=fields.get("type", ""), pipeline=fields.get("pipeline", ""),
                min_version=fields.get("min", ""), role=(fields.get("role") or "DERIVED").upper(),
                accepted=fields.get("accepted", "false").lower() in ("1", "true", "yes"),
                record_id=fields.get("id", ""), resource=fields.get("resource", ""),
                scope=(fields.get("scope") or "session").lower(), scan_type=fields.get("scan_type", ""), raw=spec)
        if not (p.resource or p.record_id or p.analysis_type or p.pipeline):
            raise ValueError(f"{PREFIX}{name}: a prerequisite needs resource=, id=, type= or pipeline= ({spec!r})")
        if p.scope not in ("session", "scan"):
            raise ValueError(f"{PREFIX}{name}: scope must be session or scan ({spec!r})")
        if p.scope == "scan" and not p.resource:
            raise ValueError(f"{PREFIX}{name}: scope=scan needs resource=<label on the scan> ({spec!r})")
        return p

    @property
    def is_resource(self) -> bool:
        return bool(self.resource)


def prerequisites_from_env(environ: dict | None = None) -> list[Prerequisite]:
    env = os.environ if environ is None else environ
    out = []
    for key in sorted(env):
        if key.startswith(PREFIX) and env[key].strip():
            out.append(Prerequisite.parse(key[len(PREFIX):], env[key]))
    return out


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v.strip()) if x != "")


def _get_json(context: XnatContext, url: str, timeout: float):
    request = urllib.request.Request(url, headers=auth_headers(context))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def list_session_records(context: XnatContext, timeout: float = 60.0) -> list[dict]:
    """Every generic session record on this session, newest first, as flat dicts with the
    record fields. The project listing is used because it carries the record columns; the
    session-scoped listing returns the session document instead of rows."""
    columns = ",".join(c if c in ("ID", "label", "insert_date") else f"{RECORD_TYPE}/{c}" for c in RECORD_COLUMNS)
    url = (f"{context.host}/data/projects/{urllib.parse.quote(context.project, safe='')}/experiments"
           f"?xsiType={RECORD_TYPE}&format=json&columns={urllib.parse.quote(columns, safe=',:/')}")
    payload = _get_json(context, url, timeout)
    rows = payload.get("ResultSet", {}).get("Result", []) if isinstance(payload, dict) else []
    key = RECORD_TYPE.lower() + "/"
    records = []
    for r in rows:
        flat = {"ID": r.get("ID"), "label": r.get("label"), "insert_date": r.get("insert_date")}
        flat.update({k[len(key):]: v for k, v in r.items() if k.startswith(key)})
        if flat.get("imagesession_id") == context.session:
            records.append(flat)
    return sorted(records, key=lambda r: r.get("insert_date") or "", reverse=True)


def choose_record(prereq: Prerequisite, records: list[dict]) -> tuple[dict | None, str]:
    """The record that satisfies ``prereq`` or (None, reason). Explicit id wins; else the newest
    SUCCEEDED (and ACCEPTED when required) record of the type/pipeline at or above min version."""
    if prereq.record_id:
        hit = next((r for r in records if r.get("ID") == prereq.record_id), None)
        return (hit, "") if hit else (None, f"record {prereq.record_id} is not on this session")
    candidates = []
    for r in records:
        if prereq.analysis_type and str(r.get("analysis_type", "")).lower() != prereq.analysis_type.lower():
            continue
        if prereq.pipeline and str(r.get("pipeline_name", "")).lower() != prereq.pipeline.lower():
            continue
        candidates.append(r)
    if not candidates:
        return None, f"no {prereq.analysis_type or 'any-type'}/{prereq.pipeline or 'any-pipeline'} record on this session"
    ok = [r for r in candidates if str(r.get("run_status", "")).upper() == "SUCCEEDED"]
    if not ok:
        return None, f"{len(candidates)} matching record(s) but none SUCCEEDED"
    if prereq.min_version:
        ok = [r for r in ok if _version_tuple(str(r.get("pipeline_version", "0"))) >= _version_tuple(prereq.min_version)]
        if not ok:
            return None, f"no SUCCEEDED record at version >= {prereq.min_version}"
    if prereq.accepted:
        ok = [r for r in ok if str(r.get("review_state", "")).upper() == "ACCEPTED"]
        if not ok:
            return None, "no ACCEPTED record (the card requires review before use)"
    return ok[0], ""


def download_role(context: XnatContext, record_id: str, role: str, dest: Path, timeout: float = 300.0) -> list[str]:
    """Copy every file of the record's ``out`` resource ``role`` into ``dest`` keeping the paths."""
    rid = urllib.parse.quote(record_id, safe="")
    listing = _get_json(context, f"{context.host}/data/experiments/{rid}/out/resources/{urllib.parse.quote(role, safe='')}/files?format=json", timeout)
    files = listing.get("ResultSet", {}).get("Result", []) if isinstance(listing, dict) else []
    written: list[str] = []
    for f in files:
        uri = f.get("URI") or ""
        rel = uri.split("/files/", 1)[1] if "/files/" in uri else (f.get("Name") or "")
        rel = urllib.parse.unquote(rel)
        if not rel or ".." in Path(rel).parts:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(f"{context.host}{uri}", headers=auth_headers(context))
        with urllib.request.urlopen(request, timeout=timeout) as response, open(target, "wb") as out:
            shutil.copyfileobj(response, out)
        written.append(rel)
    return written


def download_session_resource(context: XnatContext, label: str, dest: Path, timeout: float = 300.0) -> list[str]:
    sid = urllib.parse.quote(context.session, safe="")
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

    def as_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "path": self.path, "files": len(self.files)}
        if self.scans:
            d["scans"] = list(self.scans)
        if self.record:
            d["record"] = {k: self.record.get(k) for k in ("ID", "label", "pipeline_name", "pipeline_version", "review_state", "run_status")}
            d["role"] = self.role
        if self.resource:
            d["resource"] = self.resource
        if self.error:
            d["error"] = self.error
        return d


def resolve(context: XnatContext, prereqs: list[Prerequisite], records: list[dict] | None = None) -> list[Resolution]:
    """Decide, without downloading, which record or resource satisfies each prerequisite."""
    records = list_session_records(context) if records is None else records
    out: list[Resolution] = []
    scans: list[dict] | None = None
    for p in prereqs:
        if p.is_resource and p.scope == "scan":
            if context.scan:
                out.append(Resolution(name=p.name, kind="scan-resource", resource=p.resource, scans=[context.scan]))
            elif p.scan_type:
                scans = list_scans(context) if scans is None else scans
                hits = [s["ID"] for s in scans if fnmatch.fnmatchcase(s["type"], p.scan_type)]
                out.append(Resolution(name=p.name, kind="scan-resource", resource=p.resource, scans=hits,
                                      error="" if hits else f"no scan of type {p.scan_type!r} on this session "
                                                            f"(scan types: {sorted({s['type'] for s in scans}) or 'none'})"))
            else:
                out.append(Resolution(name=p.name, kind="scan-resource", resource=p.resource,
                                      error="scope=scan needs a scan-level run (PROC_SCAN_ID) or scan_type=<glob>"))
            continue
        if p.is_resource:
            out.append(Resolution(name=p.name, kind="resource", resource=p.resource))
            continue
        hit, why = choose_record(p, records)
        out.append(Resolution(name=p.name, kind="record", record=hit, role=p.role, error=why))
    return out


def materialise(context: XnatContext, resolutions: list[Resolution], output_dir: Path) -> None:
    for r in resolutions:
        if r.error:
            continue
        dest = output_dir / "prereq" / r.name
        dest.mkdir(parents=True, exist_ok=True)
        r.path = f"prereq/{r.name}"
        if r.kind == "scan-resource":
            missing = []
            for scan in r.scans:
                got = download_scan_resource(context, scan, r.resource, dest / scan)
                r.files.extend(f"{scan}/{f}" for f in got)
                if not got:
                    missing.append(scan)
            if missing:
                r.error = f"scan {', '.join(missing)}: no files in resource {r.resource}"
            continue
        if r.kind == "resource":
            r.files = download_session_resource(context, r.resource, dest)
        else:
            r.files = download_role(context, r.record["ID"], r.role, dest)
        if not r.files:
            r.error = f"{r.kind} {r.resource or r.record.get('ID')} has no files in {r.role or 'the resource'}"


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
    pipeline = os.environ.get("PROC_PIPELINE_NAME") or contract.card_id or "run"
    version = os.environ.get("PROC_PIPELINE_VERSION", "")
    reasons = "; ".join(f"{r.name}: {r.error}" for r in resolutions if r.error)
    label = collection_label(pipeline, context.scan, session_label=fetch_session_label(context)) + "_record"
    facts = {"wrapup": "record-fetch", "run_status": "FAILED", "auto_qc": "FAIL",
             "notes": f"Not run: prerequisite(s) unmet at setup: {reasons}. No compute ran; recorded by record-fetch {__version__}.",
             "inputs": {"scan": context.scan, "stage": "setup", "prerequisites": [r.as_dict() for r in resolutions]}}
    files = {"PROVENANCE": [output_dir / MANIFEST]}
    try:
        xml = build_record_xml(context, contract, label, {"model": pipeline, "model_version": version, "scan": context.scan},
                               [], files, False, output_dir=output_dir, facts=facts)
        record = publish_record(context, label, xml, files, output_dir=output_dir)
    except (RuntimeError, ValueError, OSError) as error:
        logger.error("failure record %s not published: %s: %s", label, type(error).__name__, error, exc_info=True)
        return {"label": label, "error": f"{type(error).__name__}: {error}"}
    logger.info("failure recorded as analysis record %s (%s): %s", record["id"], label, reasons)
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
        logger.error("prerequisites declared but the XNAT context is incomplete (XNAT_HOST/USER/PASS, PROC_PROJECT, PROC_SESSION_ID)")
        return 2
    try:
        try:
            resolutions = resolve(context, prereqs)
            materialise(context, resolutions, args.output)
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
