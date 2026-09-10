"""proc-wrapup: the generic wrapup for any card (plan Phase 3, James 2026-09-06).

It interprets nothing. For the tool that just ran it keeps everything the tool wrote
(``raw/``), captures how the run went (``status.json``, the Container Service logs and
timing), writes a report a reviewer can read, and publishes the ``analysis:sessionAnalysisData``
record that carries all of it. Masks, DICOM SEG and ROI collections are not its business:
seg-wrapup measures segmentations, and viewer products come from a converter card.

Environment (all optional beyond the XNAT context the Container Service injects):
``PROC_PIPELINE_NAME`` / ``PROC_PIPELINE_VERSION`` (falls back to ``SEG_MODEL_NAME`` /
``SEG_MODEL_VERSION``, then to ``XNW_CARD_ID``), ``PROC_PROJECT`` / ``PROC_SESSION_ID`` /
``PROC_SCAN_ID`` (or the ``SEG_*`` names), the ``XNW_*`` results contract.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import shutil
import urllib.error
import sys
import urllib.parse
from pathlib import Path

from . import __version__
from .card import write_card_copy
from .execution import (RAW_DIRNAME, STATUS_FILENAME, chain_from_workflow, copy_raw_output, fetch_parent_logs,
                        own_workflow_id, read_status, run_status_from)
from .prereq import MANIFEST as PREREQ_MANIFEST
from .publish import RecordContract, publish_if_possible
from .prereq import list_subject_sessions
from .register import XnatContext, close_session, collection_label, fetch_target_label

logger = logging.getLogger(__name__)

#: The record's fixed resources are the wrapup's own artefacts by role; ``DERIVED`` is the tool's
#: output tree (kept locally under ``raw/``, published at the resource root). A card names view
#: roles such as ``METRICS`` with globs relative to that tree (``sub-*/**/*.json``); a view is a
#: list of DERIVED paths in ``wrapup.json``, never a copy. ``status.json`` (the card's exit trap)
#: and ``prereq.json`` (record-fetch's manifest, copied in by the card) are written into the
#: tool's ``/output`` but are not the tool's: they are lifted out of the tree into PROVENANCE.
PROC_DEFAULT_RESOURCES: dict[str, list[str]] = {
    "REPORT": ["report.html"],
    "PROVENANCE": ["wrapup.json", STATUS_FILENAME, PREREQ_MANIFEST, "card/**/*"],
    "LOGS": ["logs/*.log"],
}

#: Files at the root of the tool's output that the card's command line, not the tool, wrote.
NOT_THE_TOOLS = (STATUS_FILENAME, PREREQ_MANIFEST)


def build_parser() -> argparse.ArgumentParser:
    env = os.environ
    parser = argparse.ArgumentParser(prog="proc-wrapup", description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", type=Path, default=Path(env.get("PROC_INPUT", env.get("SEG_INPUT", "/input"))))
    parser.add_argument("--output", type=Path, default=Path(env.get("PROC_OUTPUT", env.get("SEG_OUTPUT", "/output"))))
    parser.add_argument("--pipeline", default=env.get("PROC_PIPELINE_NAME") or env.get("SEG_MODEL_NAME") or env.get("XNW_CARD_ID") or "unknown")
    parser.add_argument("--pipeline-version", default=env.get("PROC_PIPELINE_VERSION") or env.get("SEG_MODEL_VERSION") or env.get("XNW_CARD_REVISION") or "unknown")
    parser.add_argument("--no-publish", action="store_true", default=env.get("PROC_NO_PUBLISH", env.get("SEG_NO_PUBLISH", "")).lower() in ("1", "true", "yes"))
    parser.add_argument("--record-label", default=env.get("PROC_RECORD_LABEL", env.get("SEG_RECORD_LABEL", "")))
    parser.add_argument("--scan", default=env.get("PROC_SCAN_ID", env.get("SEG_SCAN_ID", "")))
    parser.add_argument("--pointer-only", action="store_true", default=env.get("PROC_POINTER_ONLY", "").lower() in ("1", "true", "yes"),
                        help="after publishing, leave only wrapup.json in the output so the session gets a one-file pointer resource and the record owns the data")
    return parser


#: Where the tool's tree is, seen from the report: the record page renders ``report.html`` with a
#: ``<base>`` at ``.../resources/REPORT/files/``, so the tool's own reports are two levels up.
DERIVED_HREF = "../../DERIVED/files/"


def render_report(facts: dict) -> str:
    """One page: what ran, how it ended, what it wrote, what it said. No numbers interpreted.

    The tool's own HTML reports are linked where they live, inside ``DERIVED`` next to the
    figures they reference by relative path; a copy elsewhere would render without them."""
    e = html.escape
    rows = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in facts["summary"].items())
    files = "".join(f"<li><code>{e(f['path'])}</code> <span class='muted'>{f['size']:,} B</span></li>" for f in facts["files"])
    tool_reports = "".join(f'<li><a href="{e(DERIVED_HREF + urllib.parse.quote(name, safe="/"))}" target="_blank">{e(name)}</a></li>'
                           for name in facts.get("tool_reports", []))
    reports = (f"<h2>The tool's own reports ({len(facts['tool_reports'])})</h2><p class='muted'>Opened from the record's "
               f"<code>DERIVED</code> resource, where their figures are.</p><ul>{tool_reports}</ul>" if tool_reports else "")
    logs = ""
    for name, tail in facts.get("log_tails", {}).items():
        logs += f"<h3>{e(name)} (last {len(tail)} lines)</h3><pre>{e(chr(10).join(tail))}</pre>"
    status_class = "ok" if facts["summary"]["run_status"] == "SUCCEEDED" else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(facts['summary']['pipeline'])} run report</title>
<style>body{{font:14px/1.4 -apple-system,Helvetica,Arial,sans-serif;max-width:960px;margin:20px auto;color:#222}}
th{{text-align:left;padding:3px 10px 3px 0;color:#555;vertical-align:top}}td{{padding:3px 0}}pre{{background:#f6f8fa;padding:8px;overflow:auto;max-height:320px;font-size:12px}}
.ok{{color:#155724}}.bad{{color:#721c24}}.muted{{color:#777;font-size:12px}}</style></head><body>
<h1>{e(facts['summary']['pipeline'])} {e(facts['summary']['pipeline_version'])} <span class="{status_class}">{e(facts['summary']['run_status'])}</span></h1>
<p class="muted">Execution report written by proc-wrapup {e(__version__)} at {e(facts['generated'])}. This page interprets nothing: the tool's own output is on this record's <code>DERIVED</code> resource, unchanged.</p>
<table>{rows}</table>
{reports}
<h2>Files in DERIVED ({len(facts['files'])})</h2><ul>{files}</ul>
{logs}
</body></html>"""


def read_prerequisites(input_dir: Path) -> list[dict]:
    """What record-fetch resolved for the parent, if the tool's input mount is visible here.
    The setup writes ``prereq.json`` into the mount the main container reads; a tool that
    copies its input tree to /output (or a card whose command line does) makes it visible to
    the wrapup. Otherwise the list is empty and the record names no upstream."""
    for candidate in (input_dir / PREREQ_MANIFEST, input_dir / "prereq" / PREREQ_MANIFEST):
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text())
                return [q for q in data.get("prerequisites", []) if isinstance(q, dict) and not q.get("error")]
            except (OSError, ValueError) as error:
                logger.warning("%s unreadable (%s); prerequisites not recorded", candidate, error)
    return []


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    input_dir, output_dir = args.input, args.output
    if not input_dir.is_dir():
        logger.error("input directory %s does not exist", input_dir)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now(dt.timezone.utc)

    status = read_status(input_dir)
    # The tool's tree, verbatim, minus the two root files the card's command line put there
    # (status.json from the exit trap, prereq.json from record-fetch): those are provenance of
    # the run, not the scientists' dataset, and go to PROVENANCE as they are (plan D20).
    copied = copy_raw_output(input_dir, output_dir, skip=tuple(input_dir / name for name in NOT_THE_TOOLS))
    for name in NOT_THE_TOOLS:
        if (input_dir / name).is_file():
            shutil.copy2(input_dir / name, output_dir / name)
    derived_names = [p[len(RAW_DIRNAME) + 1:] for p in copied]
    run_status = run_status_from(status)
    # The certificate of the run (plan D27): the card at the adopted revision, under card/.
    card = write_card_copy(output_dir, "proc-wrapup")

    # The XNAT context serves log capture as well as publishing; --no-publish suppresses only
    # the record (publish_if_possible checks the flag), never the execution state.
    context = XnatContext.from_env()
    execution = None
    chain = None
    sessions: list[dict] = []
    prerequisites = read_prerequisites(input_dir)
    try:
        if context is not None:
            execution = fetch_parent_logs(context, output_dir, status, own_workflow_id())
            if context.scope == "subject":
                # A subject-scoped run (a subject-context wrapper, the tree assembled from every
                # session of the subject): the record names the sessions it spanned.
                try:
                    sessions = list_subject_sessions(context, context.subject)
                except (urllib.error.URLError, OSError, ValueError) as error:
                    logger.warning("could not list the sessions of subject %s (%s); the record will not name them", context.subject, error)
            # The orchestration fields sit on the parent's (main) workflow; the wrapup's own
            # workflow carries none (demo02 wrk_workflowdata, 2026-09-07).
            chain = chain_from_workflow(context, (execution or {}).get("workflow_id")) or chain_from_workflow(context, own_workflow_id())
        log_tails = {}
        for name in ("stdout", "stderr"):
            path = output_dir / "logs" / f"{name}.log"
            if path.exists():
                lines = path.read_text(errors="replace").splitlines()
                log_tails[name] = lines[-50:]
        files = [{"path": name, "size": (output_dir / local).stat().st_size} for name, local in zip(derived_names, copied)]
        summary = {
            "pipeline": args.pipeline, "pipeline_version": args.pipeline_version, "run_status": run_status,
            "exit_code": (status or {}).get("exit_code", 0 if status is None else "unknown"),
            "container_image": os.environ.get("XNW_CONTAINER_IMAGE", ""), "container_digest": os.environ.get("XNW_CONTAINER_DIGEST", ""),
            "card": f"{os.environ.get('XNW_CARD_ID', '')} {os.environ.get('XNW_CARD_REVISION', '')}".strip(),
            "cs_container_id": (execution or {}).get("container_id", ""), "duration_seconds": (execution or {}).get("duration_seconds", ""),
            "files_kept": len(copied),
            "chain": (f"orchestration {chain['orchestration_id']} step {chain['step']} job {chain['job_id']}" if chain else ""),
            "node": (execution or {}).get("facts", {}).get("node_id", ""), "backend": (execution or {}).get("facts", {}).get("backend", ""),
            "envelope": json.dumps((execution or {}).get("facts", {}).get("envelope", {})),
            "total_seconds": (execution or {}).get("facts", {}).get("total_seconds", ""),
            "prerequisites": ", ".join(f"{q['name']}={q.get('record', {}).get('ID') or q.get('resource', '')}" for q in prerequisites),
            "scope": context.scope if context else "session",
            "sessions": ", ".join(s["label"] for s in sessions),
        }
        facts = {"summary": summary, "files": files, "log_tails": log_tails, "generated": started.strftime("%Y-%m-%d %H:%M:%S UTC"),
                 "tool_reports": [name for name in derived_names if name.lower().endswith((".html", ".htm"))]}
        (output_dir / "report.html").write_text(render_report(facts))
        manifest = {"wrapup": "proc-wrapup", "version": __version__, "generated": facts["generated"], "pipeline": args.pipeline,
                    "pipeline_version": args.pipeline_version, "run_status": run_status, "status": status, "execution": execution,
                    "chain": chain, "prerequisites": prerequisites, "derived_root": RAW_DIRNAME, "raw_files": copied,
                    "scope": context.scope if context else "session", "sessions": sessions, "card": card}
        (output_dir / "wrapup.json").write_text(json.dumps(manifest, indent=2))

        report = {"model": args.pipeline, "model_version": args.pipeline_version, "scan": args.scan}
        record_facts = {"wrapup": "proc-wrapup", "run_status": run_status, "auto_qc": "FAIL" if run_status == "FAILED" else "NOT_EVALUATED",
                        "container_id": (execution or {}).get("container_id"), "duration_seconds": (execution or {}).get("duration_seconds"),
                        "config": (execution or {}).get("facts"),
                        "notes": f"Published by proc-wrapup {__version__}; tool output kept verbatim under {RAW_DIRNAME}/; nothing interpreted",
                        "inputs": {"scan": args.scan, "status_json": status is not None, "raw_files": len(copied),
                                   "scope": context.scope if context else "session",
                                   "subject": context.subject if context else "",
                                   "sessions": sessions,
                                   "chain": chain,
                                   "prerequisites": [{"name": q["name"], "record": (q.get("record") or {}).get("ID"),
                                                      "resource": q.get("resource"), "role": q.get("role")} for q in prerequisites],
                                   "upstream_record": next(((q.get("record") or {}).get("ID") for q in prerequisites if q.get("record")), None)}}
        if not (args.record_label or "").strip():
            args.record_label = collection_label(args.pipeline, args.scan,
                                                 session_label=fetch_target_label(context) if context else "") + "_record"
        outcome = publish_if_possible(args, output_dir, report, [], False, context=context, facts=record_facts,
                                      default_resources=PROC_DEFAULT_RESOURCES, derived_root=RAW_DIRNAME, manifest=manifest)
        # Local paths of what went up (and of the empty files that could not): for the pointer
        # reduction below, not for the manifest, which already lists the record's names.
        output_paths = (outcome or {}).pop("output_paths", None) or {}
        manifest["analysis_record"] = outcome
        (output_dir / "wrapup.json").write_text(json.dumps(manifest, indent=2))
        if args.pointer_only and (manifest.get("analysis_record") or {}).get("id"):
            # The record owns the bytes; the output handler gets a one-file pointer resource.
            # Only files the record confirmed uploaded are removed (Codex P1, PR #10), plus the
            # zero-byte files the record could not take: XNAT refuses them in-body, and a file the
            # publisher skipped must not be uploaded anywhere else either (0.5.0 left them in the
            # pointer resource: XNW_FMRIPREP on demo02 carried an empty stderr.log and patchdir.txt).
            removable = set(output_paths.get("uploaded", [])) | set(output_paths.get("skipped_empty", []))
            kept = []
            for path in sorted(output_dir.rglob("*"), reverse=True):
                if path.is_file() and path != output_dir / "wrapup.json":      # only the root manifest is the pointer
                    if path.relative_to(output_dir).as_posix() in removable:
                        path.unlink()
                    else:
                        kept.append(path.relative_to(output_dir).as_posix())
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            if kept:
                logger.warning("pointer-only: %d file(s) are not on record %s (outside the contract) and stay in the output: %s",
                               len(kept), manifest["analysis_record"]["id"], ", ".join(sorted(kept)))
            else:
                logger.info("pointer-only: output reduced to wrapup.json; record %s holds the files", manifest["analysis_record"]["id"])
    finally:
        if context is not None:
            close_session(context)
    logger.info("proc-wrapup done: %s, %d raw file(s), record %s", run_status, len(copied),
                (manifest.get("analysis_record") or {}).get("id") if 'manifest' in dir() else None)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
