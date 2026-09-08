"""Execution state of the parent run, kept with its output (plan D10, James 2026-09-06).

Three things a reviewer needs that the tool's files alone do not give: *everything* the tool
wrote (not just what a wrapup recognises), *how the run ended*, and *what it said* on the
way. This module gathers them for any card:

- ``copy_raw_output``: the parent's whole ``/input`` tree into ``/output/raw/``, so the scan
  resource and the record's ``DERIVED`` carry it. The one entry left out is the DICOM copy the
  card put at ``.source_dicom`` (XNAT already holds it); the tool's own dotfiles (``.bidsignore``,
  ``.heudiconv/``) are part of the dataset and are kept (0.6.1; until 0.6.0 every dot-prefixed
  entry was dropped).
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

#: Where a parent command leaves the source DICOM for the wrapup (``/output/.source_dicom`` in
#: the parent, so ``/input/.source_dicom`` here). The wrapup consumes it for the DICOM SEG; it
#: is never copied on or uploaded, because XNAT already holds that series.
SOURCE_DICOM_DIRNAME = ".source_dicom"

#: Root entries of a tool's output tree that are the wrapup contract's, not the tool's, and never
#: join the dataset. Matched by exact name as the first path component under the tree root and
#: nowhere else: plan D20 makes DERIVED the scientists' dataset byte-for-byte and path-for-path,
#: so the wrapup enumerates what it reserves instead of judging the tree (a dot-prefix rule
#: dropped ``.bidsignore`` and ``.heudiconv/`` until 0.6.0). ``status.json`` and ``prereq.json``
#: are the card's too but are lifted into PROVENANCE by proc-wrapup, not skipped here.
RESERVED_ROOT_NAMES = frozenset({SOURCE_DICOM_DIRNAME})


def is_reserved(relative: Path) -> bool:
    """True when ``relative`` (a path under a tree root) is, or is inside, a reserved root entry."""
    return bool(relative.parts) and relative.parts[0] in RESERVED_ROOT_NAMES


def copy_raw_output(input_dir: Path, output_dir: Path, skip: tuple[Path, ...] = ()) -> list[str]:
    """Copy every file under ``input_dir`` to ``output_dir/raw/`` keeping the tree, dotfiles included.

    Left out: the reserved root entries (:data:`RESERVED_ROOT_NAMES`, the DICOM copy) and
    ``skip``, files or directories the caller names (a wrapup that already placed the masks at
    the top level passes them here so nothing is uploaded twice). Returns the relative paths.
    """
    skipped = tuple(p.resolve() for p in skip)
    copied: list[str] = []
    raw_root = output_dir / RAW_DIRNAME
    for path in sorted(input_dir.rglob("*")):
        rel = path.relative_to(input_dir)
        if is_reserved(rel) or not path.is_file():
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


def _stamps(container: dict) -> list[tuple[str, dt.datetime]]:
    """(status, time) pairs from the CS history, oldest first; unparseable entries dropped."""
    stamps: list[tuple[str, dt.datetime]] = []
    for entry in container.get("history") or container.get("status-history") or []:
        when = entry.get("time-recorded") or entry.get("external-timestamp")
        if not when:
            continue
        try:
            stamps.append((str(entry.get("status") or "").lower(), dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))))
        except ValueError:
            continue
    return sorted(stamps, key=lambda s: s[1])


def _first(stamps: list[tuple[str, dt.datetime]], *statuses: str, after: dt.datetime | None = None) -> dt.datetime | None:
    return next((t for status, t in stamps if status in statuses and (after is None or t >= after)), None)


def _duration_seconds(container: dict) -> int | None:
    """Seconds the parent actually ran: from the docker 'running' event to the docker
    'complete'/'failed' event when the CS history has them, else first to last entry (which
    also counts the time the container sat Created and Finalizing)."""
    stamps = _stamps(container)
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


def _phase(container: dict | None, now: dt.datetime | None = None) -> dict | None:
    """created / started / finished and the seconds between, for one CS container."""
    if not container:
        return None
    stamps = _stamps(container)
    created = _first(stamps, "created")
    started = _first(stamps, "running")
    timed_from = "running"
    if started is None and created is not None:
        # Short swarm tasks (a setup that finishes in seconds) get no docker 'running' event in
        # the CS history; Created -> complete is then the honest upper bound (demo02, 2026-09-07).
        started, timed_from = created, "created"
    finished = _first(stamps, "complete", "failed", "done", "die", after=started) if started else None
    out = {"container_id": container.get("id"), "timed_from": timed_from,
           "created": created.isoformat() if created else None, "started": started.isoformat() if started else None,
           "finished": finished.isoformat() if finished else None,
           "seconds": int((finished - started).total_seconds()) if started and finished else None,
           "envelope": _envelope(container)}
    if started and not finished and now is not None:
        # The wrapup itself: still running while it writes this, so its end is unknown. Record
        # what has elapsed and say so; the CS history holds the final figure (Codex P2, PR #10).
        out["in_progress"] = True
        out["elapsed_seconds_at_record"] = int((now - started).total_seconds())
    if created and started and timed_from == "running":
        out["queue_wait_seconds"] = int((started - created).total_seconds())
    return out


def _envelope(container: dict) -> dict:
    """What the card reserved for this container; every phase has its own (setup and wrapup
    images declare different limits from the tool)."""
    return {"reserve_memory_mib": container.get("reserve-memory"), "limit_memory_mib": container.get("limit-memory"),
            "limit_cpu": container.get("limit-cpu"), "generic_resources": container.get("generic-resources") or {},
            "swarm_constraints": container.get("swarm-constraints") or []}


def describe_execution(containers: list, parent: dict, own: dict | None, now: dt.datetime | None = None) -> dict:
    """The audit and billing facts of one run, from what the Container Service holds
    (James, 2026-09-07: "accurate compute time … plus machine that ran it"): where it ran,
    the envelope the card reserved, and the wall clock of each phase. Setup containers are
    the ones that wrote the parent's input mount; the wrapup is ``own``. Reserved resources,
    not measured usage: the CS records no cgroup accounting."""
    inputs = _mount_paths(parent, writable=False)
    setups = [c for c in containers if isinstance(c, dict) and c.get("subtype") == "docker-setup" and _mount_paths(c, writable=True) & inputs]
    phases = {"setup": [_phase(c) for c in setups], "main": _phase(parent), "wrapup": _phase(own, now=now)}
    finished_phases = [p for p in ([phases["main"]] + phases["setup"]) if p and p.get("seconds") is not None]
    total = sum(p["seconds"] for p in finished_phases)
    return {"backend": parent.get("backend"), "node_id": parent.get("node-id"), "service_id": parent.get("service-id"),
            "task_id": parent.get("task-id"), "docker_container_id": parent.get("container-id"),
            "image": parent.get("docker-image"), "user": parent.get("user-id"),
            "envelope": _envelope(parent), "phases": phases,
            "total_seconds": total,
            "billing_note": ("total_seconds = setup + main wall clock; each phase carries its own reserved envelope; the wrapup "
                             "is still running when this is written (elapsed_seconds_at_record), its final time is in the "
                             "Container Service history; no measured usage is recorded")}


def find_parent_container(context: XnatContext, workflow_id: str | None, own_workflow_id: str | None,
                          timeout: float = 60.0, found: dict | None = None) -> dict | None:
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
    own = by_workflow.get(str(own_workflow_id)) if own_workflow_id else None
    if found is not None:                       # the caller wants the list and the wrapup's own container too
        found["containers"] = containers
        found["own"] = own
    if workflow_id and str(workflow_id) in by_workflow:
        return by_workflow[str(workflow_id)]
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
    found: dict = {}
    parent = find_parent_container(context, (status or {}).get("workflow_id"), own_workflow_id, timeout, found=found)
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
            "workflow_id": parent.get("workflow-id"), "duration_seconds": _duration_seconds(parent), "logs": written,
            "facts": describe_execution(found.get("containers") or [], parent, found.get("own"), now=dt.datetime.now(dt.timezone.utc))}
    logger.info("execution state from Container Service container %s: %s, %s s", info["container_id"], info["status"], info["duration_seconds"])
    return info


def chain_from_workflow(context: XnatContext, workflow_id: str | None, timeout: float = 60.0) -> dict | None:
    """The orchestration this run belongs to, from the wrapup's own workflow: the Container
    Service stores ``nextStepId`` (orchestration id), ``currentStepId`` (step index) and
    ``jobid`` (shared bulk-launch id) on every orchestrated workflow
    (ContainerServiceImpl.createContainerWorkflow, ContainerServiceWorkflowStatusEventListener).
    None when the run is not orchestrated or the workflow cannot be read."""
    if not workflow_id:
        return None
    try:
        payload = _get_json(context, f"{context.host}/data/workflows/{workflow_id}?format=json", timeout)
        fields = payload["items"][0]["data_fields"]
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as error:
        logger.warning("could not read workflow %s for chain provenance: %s", workflow_id, error)
        return None
    lower = {str(k).lower(): v for k, v in fields.items()}
    orchestration = lower.get("next_step_id") or lower.get("nextstepid")
    if not orchestration:
        return None
    return {"orchestration_id": str(orchestration), "step": int(str(lower.get("current_step_id") or lower.get("currentstepid") or 0) or 0),
            "job_id": str(lower.get("jobid") or lower.get("job_id") or ""), "workflow_id": str(workflow_id)}


def own_workflow_id(environ: dict | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return (env.get("XNAT_WORKFLOW_ID") or "").strip() or None
