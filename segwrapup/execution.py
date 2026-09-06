"""Execution state of the parent run, kept with its output (plan D10, James 2026-09-06).

Three things a reviewer needs that the tool's files alone do not give: *everything* the tool
wrote (not just what a wrapup recognises), *how the run ended*, and *what it said* on the
way. This module gathers them for any card:

- ``copy_raw_output``: the parent's whole ``/input`` tree into ``/output/raw/``, so the scan
  resource and the record's ``DERIVED`` carry it. Hidden entries are skipped: ``.source_dicom``
  is the DICOM XNAT already holds.
- ``read_status``: the parent's ``status.json`` when its command line trapped a failure
  (the Container Service never runs a wrapup after a non-zero exit, so a card that wants a
  record on failure writes this file and exits 0).
- ``fetch_parent_logs``: the parent container's stdout/stderr and timing from the Container
  Service, found through the workflow id ``status.json`` carries, or through the mounts: the
  Container Service resolves the wrapup's ``/input`` from the parent's output mount, so the
  two containers share one ``xnat-host-path`` (verified on demo02, containers 35531/35532).
  The wrapup's own record is found by ``XNAT_WORKFLOW_ID``. Written to ``/output/logs/``.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from .register import XnatContext, auth_headers

logger = logging.getLogger(__name__)

RAW_DIRNAME = "raw"
LOGS_DIRNAME = "logs"
STATUS_FILENAME = "status.json"


def copy_raw_output(input_dir: Path, output_dir: Path, skip: tuple[Path, ...] = ()) -> list[str]:
    """Copy every non-hidden file under ``input_dir`` to ``output_dir/raw/`` keeping the tree.

    ``skip`` names files or directories not to copy (a wrapup that already placed the masks at
    the top level passes them here so nothing is uploaded twice). Returns the relative paths.
    """
    skipped = tuple(p.resolve() for p in skip)
    copied: list[str] = []
    raw_root = output_dir / RAW_DIRNAME
    for path in sorted(input_dir.rglob("*")):
        rel = path.relative_to(input_dir)
        if any(part.startswith(".") for part in rel.parts) or not path.is_file():
            continue
        resolved = path.resolve()
        if any(resolved == s or s in resolved.parents for s in skipped):
            continue
        destination = raw_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append((Path(RAW_DIRNAME) / rel).as_posix())
    if copied:
        logger.info("kept %d file(s) of the tool's own output under %s/", len(copied), RAW_DIRNAME)
    return copied


def read_status(input_dir: Path) -> dict | None:
    """The parent's ``status.json`` ({"exit_code": N, "workflow_id": "…", …}) or None."""
    path = input_dir / STATUS_FILENAME
    if not path.exists():
        return None
    try:
        status = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        logger.warning("%s is unreadable (%s); execution state unknown", path, error)
        return {"error": f"unreadable status.json: {error}"}
    if not isinstance(status, dict):
        logger.warning("%s is not a JSON object; execution state unknown", path)
        return {"error": "status.json is not an object"}
    return status


def run_status_from(status: dict | None) -> str:
    """SUCCEEDED unless the parent recorded a non-zero exit. A wrapup only runs after the
    Container Service saw exit 0, so without a status file the run did succeed."""
    if not status or status.get("exit_code") in (None, "", 0, "0"):
        return "SUCCEEDED"
    return "FAILED"


def _get_json(context: XnatContext, url: str, timeout: float):
    request = urllib.request.Request(url, headers=auth_headers(context))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _get_text(context: XnatContext, url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers=auth_headers(context))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode(errors="replace")


def _duration_seconds(container: dict) -> int | None:
    """Seconds the parent actually ran: from the docker 'running' event to the docker
    'complete'/'failed' event when the CS history has them, else first to last entry (which
    also counts the time the container sat Created and Finalizing)."""
    stamps: list[tuple[str, dt.datetime]] = []
    for entry in container.get("history") or []:
        when = entry.get("time-recorded") or entry.get("external-timestamp")
        if not when:
            continue
        try:
            stamps.append((str(entry.get("status") or "").lower(), dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))))
        except ValueError:
            continue
    if len(stamps) < 2:
        return None
    started = next((t for status, t in stamps if status == "running"), None)
    ended = next((t for status, t in stamps if status in ("complete", "failed", "done", "die") and started and t >= started), None)
    if started and ended:
        return int((ended - started).total_seconds())
    times = [t for _, t in stamps]
    return int((max(times) - min(times)).total_seconds())


def _mount_paths(container: dict, writable: bool | None) -> set[str]:
    """xnat-host-paths of a container's mounts, optionally only the writable (output) or read-only (input) ones."""
    return {str(m.get("xnat-host-path")) for m in container.get("mounts") or []
            if isinstance(m, dict) and m.get("xnat-host-path") and (writable is None or bool(m.get("writable")) == writable)}


HELPER_SUBTYPES = ("docker-wrapup", "docker-setup")


def find_parent_container(context: XnatContext, workflow_id: str | None, own_workflow_id: str | None,
                          timeout: float = 60.0) -> dict | None:
    """The parent (main) container: by the workflow id the parent recorded in status.json, else
    by the mounts. The Container Service hides the parent link from its REST model, but it
    resolves the wrapup's input mount from the parent's output mount, so the wrapup's own
    container (found by ``XNAT_WORKFLOW_ID``) and the parent share an ``xnat-host-path``.
    Workflow ids are not used for adjacency: on a busy server the id before the wrapup's own
    belongs to whatever launched while the parent ran."""
    try:
        containers = _get_json(context, f"{context.host}/xapi/containers", timeout)
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as error:
        logger.warning("could not list containers to find the parent run: %s", error)
        return None
    if not isinstance(containers, list):
        logger.warning("container list is not a list; parent run not found")
        return None
    by_workflow = {str(c.get("workflow-id")): c for c in containers if isinstance(c, dict) and c.get("workflow-id")}
    if workflow_id and str(workflow_id) in by_workflow:
        return by_workflow[str(workflow_id)]
    own = by_workflow.get(str(own_workflow_id)) if own_workflow_id else None
    inputs = _mount_paths(own, writable=False) if own else set()
    if inputs:
        parents = [c for c in containers if isinstance(c, dict) and c is not own
                   and c.get("subtype") not in HELPER_SUBTYPES and _mount_paths(c, writable=True) & inputs]
        if len(parents) == 1:
            return parents[0]
        if parents:
            logger.warning("%d containers write to the wrapup's input mount %s; parent ambiguous, LOGS skipped",
                           len(parents), sorted(inputs))
            return None
    logger.info("parent container not found (status.json workflow_id=%s, own=%s, own container %s); LOGS skipped",
                workflow_id, own_workflow_id, "found" if own else "not found")
    return None


def fetch_parent_logs(context: XnatContext, output_dir: Path, status: dict | None,
                      own_workflow_id: str | None = None, timeout: float = 120.0) -> dict | None:
    """Write the parent's stdout/stderr to ``output_dir/logs/`` and return what CS knows about it."""
    parent = find_parent_container(context, (status or {}).get("workflow_id"), own_workflow_id, timeout)
    if parent is None:
        return None
    logs_dir = output_dir / LOGS_DIRNAME
    logs_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for stream in ("stdout", "stderr"):
        try:
            text = _get_text(context, f"{context.host}/xapi/containers/{parent['id']}/logs/{stream}", timeout)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
            logger.warning("could not fetch %s of container %s: %s", stream, parent["id"], error)
            continue
        (logs_dir / f"{stream}.log").write_text(text)
        written.append(f"{LOGS_DIRNAME}/{stream}.log")
    info = {"container_id": parent.get("id"), "status": parent.get("status"), "docker_image": parent.get("docker-image"),
            "workflow_id": parent.get("workflow-id"), "duration_seconds": _duration_seconds(parent), "logs": written}
    logger.info("execution state from Container Service container %s: %s, %s s", info["container_id"], info["status"], info["duration_seconds"])
    return info


def own_workflow_id(environ: dict | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return (env.get("XNAT_WORKFLOW_ID") or "").strip() or None
