"""proc-wrapup: keep everything the tool wrote, capture how the run went, report, publish.

A fake XNAT + Container Service on localhost records every request (same style as
test_publish.py) and answers the container list and log endpoints.
"""
import html
import json
import re
import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from segwrapup import __version__, proc
from segwrapup.execution import copy_raw_output, read_status, run_status_from

CONTRACT_ENV = {"XNW_CARD_ID": "pyradiomics", "XNW_CARD_REVISION": "1.1.0", "XNW_CONTRACT_VERSION": "0.1",
                "XNW_ANALYSIS_TYPE": "radiomics", "XNW_CONTAINER_IMAGE": "radiomics/pyradiomics:CLI",
                "XNW_CONTAINER_DIGEST": "sha256:" + "b" * 64, "XNW_OUTPUT_RESOURCE_LABEL": "PyRadiomics",
                "XNW_RESOURCE_METRICS": "features.csv"}     # a view glob, relative to the DERIVED root (0.6.0)
CONTEXT_ENV = {"XNAT_HOST": "http://x", "XNAT_USER": "alias", "XNAT_PASS": "secret",
               "PROC_PROJECT": "PROJ_1", "PROC_SESSION_ID": "XNAT_E00018", "PROC_SCAN_ID": "3", "XNAT_WORKFLOW_ID": "5001"}
# The parent (900) ran under workflow 4990; a stranger's run (902) took workflow 5000, the id
# just before the wrapup's own 5001, so any "previous workflow id" guess picks the wrong run.
# The real link is the mount: CS resolves the wrapup's /input from the parent's output mount
# (same xnat-host-path on both), as seen on demo02 for containers 35531/35532.
CARD_BLOCK = {"id": "pyradiomics", "version": "0.3.1", "name": "PyRadiomics", "dockerImage": "radiomics/pyradiomics:CLI", "imageDigest": "sha256:" + "b" * 64,
              "license": "BSD-3-Clause", "url": "https://github.com/mrjamesdickson/container-workshop/tree/main/wrappers/pyradiomics"}
BUILD = "/data/xnat/build/1727a620-4b73-4aad-872d-681a14a98d77"
SETUP_OUT = "/data/xnat/build/0c0c-setup-output"
CONTAINERS = [
    {"id": 900, "workflow-id": "4990", "subtype": "docker", "status": "Complete", "docker-image": "radiomics/pyradiomics:CLI", "command-id": 77, "wrapper-id": 88,
     "backend": "swarm", "node-id": "laz2ephdgvajpbg96rhc6lfmn", "service-id": "svc1", "task-id": "task1", "container-id": "5687d46dcb7c", "user-id": "admin",
     "reserve-memory": 256, "limit-memory": 1024, "limit-cpu": 1.0, "generic-resources": {"GPU": "1"},
     "mounts": [{"name": "input-mount", "writable": False, "xnat-host-path": SETUP_OUT},
                {"name": "output-mount", "writable": True, "xnat-host-path": BUILD}],
     "history": [{"status": "Created", "time-recorded": "2026-09-06T10:00:00.000+0000"},
                 {"status": "running", "time-recorded": "2026-09-06T10:00:20.000+0000"},
                 {"status": "complete", "time-recorded": "2026-09-06T10:02:20.000+0000"},
                 {"status": "Complete", "time-recorded": "2026-09-06T10:02:30.000+0000"}]},
    {"id": 899, "workflow-id": "4991", "subtype": "docker-setup", "status": "Complete", "docker-image": "xnatworks/record-fetch:0.5.0",
     "reserve-memory": 1024, "limit-memory": 4096, "limit-cpu": 2.0,
     "mounts": [{"name": "input", "writable": False, "xnat-host-path": "/data/xnat/archive/P/arc001/S/SCANS/3/NIFTI"},
                {"name": "output", "writable": True, "xnat-host-path": SETUP_OUT}],
     "history": [{"status": "Created", "time-recorded": "2026-09-06T09:59:50.000+0000"},
                 {"status": "running", "time-recorded": "2026-09-06T09:59:52.000+0000"},
                 {"status": "complete", "time-recorded": "2026-09-06T09:59:58.000+0000"}]},
    {"id": 902, "workflow-id": "5000", "subtype": "docker", "status": "Complete", "docker-image": "someone/else:1",
     "mounts": [{"name": "output-mount", "writable": True, "xnat-host-path": "/data/xnat/build/other-run"}],
     "history": [{"status": "Created", "time-recorded": "2026-09-06T10:01:00.000+0000"},
                 {"status": "Complete", "time-recorded": "2026-09-06T10:01:05.000+0000"}]},
    {"id": 901, "workflow-id": "5001", "subtype": "docker-wrapup", "status": "Running", "docker-image": "xnatworks/proc-wrapup:0.4.0",
     "mounts": [{"name": "input", "writable": False, "xnat-host-path": BUILD},
                {"name": "output", "writable": True, "xnat-host-path": "/data/xnat/build/f4f49ad1"}],
     "history": [{"status": "Created", "time-recorded": "2026-09-06T10:02:31.000+0000"},
                 {"status": "running", "time-recorded": "2026-09-06T10:02:33.000+0000"}]},
]


class _CS(BaseHTTPRequestHandler):
    calls: list = []

    def _record(self, body=b""):
        _CS.calls.append({"method": self.command, "path": self.path, "cookie": self.headers.get("Cookie"),
                          "auth": self.headers.get("Authorization"), "body": body})

    def _send(self, status, body=b"", ctype="text/plain"):
        self.send_response(status); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        self._record()
        self._send(200, b"FAKESESSION") if self.path == "/data/JSESSION" else self._send(500)

    def do_DELETE(self):
        self._record(); self._send(200)

    truncate_logs = False
    containers = CONTAINERS

    def do_GET(self):
        self._record()
        if self.path == "/xapi/containers":
            self._send(200, json.dumps(_CS.containers).encode(), "application/json")
        elif self.path == "/xapi/commands/77":                       # the registered command carries the card (plan D27)
            self._send(200, json.dumps({"id": 77, "name": "pyradiomics", "version": "0.3.1", "command-metadata": {"card": CARD_BLOCK}}).encode(), "application/json")
        elif self.path == "/xapi/users/username":                        # the alias token resolves to the real login
            self._send(200, b"jdickson")
        elif self.path == "/data/experiments/XNAT_E77777?format=json":        # the run record, re-read before results_json.trained_model is written
            self._send(200, json.dumps({"items": [{"data_fields": {"ID": "XNAT_E77777", "label": "run_label_x", "project": "PROJ_1",
                                                                    "results_json": json.dumps({"views": {"MODEL": ["segmentation_spleen.pt"]}, "model": "monailabel-train"})}}]}).encode(), "application/json")
        elif self.path == "/data/experiments/XNAT_E00018?format=json":
            self._send(200, json.dumps({"items": [{"data_fields": {"label": "SESS01"}}]}).encode(), "application/json")
        elif self.path == "/data/experiments/XNAT_D0001?format=json":        # the frozen dataset a dataset-scoped run cites
            self._send(200, json.dumps({"items": [{"data_fields": {"label": "flanker-2sub", "included_count": 2,
                                                                    "xsiType": "analysis:analysisDatasetData"}}]}).encode(), "application/json")
        elif self.path == "/data/projects/PROJ_1/subjects/XNAT_S09007?format=json":
            self._send(200, json.dumps({"items": [{"data_fields": {"label": "292"}}]}).encode(), "application/json")
        elif self.path.startswith("/data/projects/PROJ_1/subjects/XNAT_S09007/experiments?format=json"):
            rows = [{"ID": "XNAT_E25641", "label": "292_postop", "xsiType": "xnat:mrSessionData"},
                    {"ID": "XNAT_E25642", "label": "292_preop", "xsiType": "xnat:mrSessionData"}]
            self._send(200, json.dumps({"ResultSet": {"Result": rows}}).encode(), "application/json")
        elif "/subjects/XNAT_S09007/experiments/" in self.path and self.path.endswith("?format=json"):
            self._send(404)                                                   # subject-scope label probe: free
        elif self.path == "/data/workflows/4990?format=json":       # the parent's workflow carries the orchestration fields
            self._send(200, json.dumps({"items": [{"data_fields": {"wrk_workflowData_id": 4990, "status": "Complete", "next_step_id": "42",
                                                                    "current_step_id": "2", "jobid": "job-abc"}}]}).encode(), "application/json")
        elif self.path == "/data/workflows/5001?format=json":       # the wrapup's own does not (demo02 wrk_workflowdata, 2026-09-07)
            self._send(200, json.dumps({"items": [{"data_fields": {"wrk_workflowData_id": 5001, "status": "Running"}}]}).encode(), "application/json")
        elif self.path.startswith("/xapi/containers/900/logs/") and _CS.truncate_logs:
            # a Content-Length the body never reaches: urllib raises http.client.IncompleteRead
            self.send_response(200); self.send_header("Content-Length", "4096"); self.end_headers()
            self.wfile.write(b"partial"); self.wfile.flush(); self.connection.close()
        elif self.path.startswith("/xapi/containers/900/logs/"):
            self._send(200, f"line one\\n{self.path.rsplit('/', 1)[-1]} line two\\n".encode())
        else:
            self._send(404)

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        self._record(self.rfile.read(length))
        create = (("/assessors/" in self.path and "/out/" not in self.path) or ("/subjects/" in self.path and "/resources/" not in self.path)
                  or (self.path.startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in self.path))
        model_create = create and "/experiments/model_" in self.path
        self._send(201 if create else 200, b"XNAT_E88888" if model_create else b"XNAT_E77777" if create else b"")

    def log_message(self, *args):
        pass


@pytest.fixture
def cs():
    _CS.calls = []
    _CS.truncate_logs = False
    _CS.containers = CONTAINERS
    server = HTTPServer(("127.0.0.1", 0), _CS)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", _CS
    server.shutdown()


def tool_output(tmp_path, with_status=None):
    inp = tmp_path / "in"
    (inp / "sub").mkdir(parents=True)
    (inp / "features.csv").write_text("a,b\\n1,2\\n")
    (inp / "sub" / "log.txt").write_text("hello")
    (inp / ".source_dicom").mkdir()
    (inp / ".source_dicom" / "1.dcm").write_bytes(b"\\x00")
    if with_status is not None:
        (inp / "status.json").write_text(json.dumps(with_status))
    return inp


def set_env(monkeypatch, host, extra=None, contract=True):
    for key in list(CONTRACT_ENV) + list(CONTEXT_ENV) + ["SEG_PROJECT", "SEG_SESSION_ID", "XNW_CONTRACT"]:
        monkeypatch.delenv(key, raising=False)
    env = {**CONTEXT_ENV, "XNAT_HOST": host}
    if contract:
        env.update(CONTRACT_ENV)
    env.update(extra or {})
    for k, v in env.items():
        monkeypatch.setenv(k, v)


# ── execution module ───────────────────────────────────────────────────────────

def test_copy_raw_output_keeps_the_tree_and_skips_hidden_and_skipped(tmp_path):
    inp = tool_output(tmp_path)
    out = tmp_path / "out"; out.mkdir()
    copied = copy_raw_output(inp, out, skip=(inp / "sub" / "log.txt",))
    assert copied == ["raw/features.csv"]
    assert (out / "raw" / "features.csv").read_text() == "a,b\\n1,2\\n"
    assert not (out / "raw" / ".source_dicom").exists() and not (out / "raw" / "sub").exists()


def test_copy_raw_output_keeps_the_tools_dotfiles_and_skips_only_the_dicom_copy(tmp_path):
    """qsirecon, qsiprep and fmriprep write .bidsignore; heudiconv leaves .heudiconv/ in a raw
    dataset. 0.6.0 skipped every dot-prefixed entry, so none of them reached raw/ or the record.
    The only entry that is not the tool's is the DICOM copy the card put at .source_dicom."""
    inp = tool_output(tmp_path)
    (inp / ".bidsignore").write_text("*.html\n")
    (inp / ".heudiconv" / "sub-1").mkdir(parents=True)
    (inp / ".heudiconv" / "sub-1" / "info.json").write_text("{}")
    (inp / "sub" / ".state").write_text("done")
    out = tmp_path / "out"; out.mkdir()
    copied = copy_raw_output(inp, out)
    assert copied == ["raw/.bidsignore", "raw/.heudiconv/sub-1/info.json", "raw/features.csv", "raw/sub/.state", "raw/sub/log.txt"]
    assert (out / "raw" / ".bidsignore").read_text() == "*.html\n"
    assert not (out / "raw" / ".source_dicom").exists()


def test_status_and_run_status(tmp_path):
    assert read_status(tmp_path) is None and run_status_from(None) == "SUCCEEDED"
    (tmp_path / "status.json").write_text('{"exit_code": 1, "workflow_id": "5000"}')
    status = read_status(tmp_path)
    assert status["exit_code"] == 1 and run_status_from(status) == "FAILED"
    (tmp_path / "status.json").write_text("not json")
    assert "error" in read_status(tmp_path) and run_status_from(read_status(tmp_path)) == "SUCCEEDED"


# ── end to end ─────────────────────────────────────────────────────────────────

def test_proc_wrapup_keeps_everything_captures_logs_reports_and_publishes(cs, tmp_path, monkeypatch):
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "PyRadiomics", "PROC_PIPELINE_VERSION": "3.1"})
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0

    # the certificate of the run (D27): the card block of command 77, read back from CS, under card/, never in DERIVED
    assert json.loads((out / "card" / "metadata.json").read_text()) == CARD_BLOCK
    card_json = json.loads((out / "card" / "card.json").read_text())
    assert card_json["wrapup"] == "proc-wrapup" and card_json["command_id"] == 77 and card_json["wrapper_id"] == 88 and "error" not in card_json
    assert not (out / "raw" / "card").exists()
    # everything the tool wrote, verbatim, under raw/; the DICOM copy not
    assert (out / "raw" / "features.csv").exists() and (out / "raw" / "sub" / "log.txt").exists()
    assert not (out / "raw" / ".source_dicom").exists()
    # execution state: status carried, parent logs fetched from CS by the workflow id in status.json
    assert json.loads((out / "status.json").read_text())["exit_code"] == 0
    assert (out / "logs" / "stdout.log").read_text().startswith("line one") and (out / "logs" / "stderr.log").exists()
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["wrapup"] == "proc-wrapup" and manifest["run_status"] == "SUCCEEDED"
    assert manifest["execution"]["container_id"] == 900 and manifest["execution"]["duration_seconds"] == 120
    # a report a reviewer can read, interpreting nothing; files named as they are on DERIVED
    report = (out / "report.html").read_text()
    assert "PyRadiomics 3.1" in report and "SUCCEEDED" in report and "features.csv" in report and "line one" in report
    assert "raw/features.csv" not in report
    # the record: generic fields, the wrapup's artefacts by fixed role, the tool's tree as DERIVED, one session
    creates = [c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"]]
    # the label carries the session label: labels are unique per project, not per session
    assert len(creates) == 1 and creates[0]["path"].startswith("/data/experiments/XNAT_E00018/assessors/PyRadiomics_SESS01_scan3_")
    xml = creates[0]["body"].decode()
    for fragment in ("<analysis:pipeline_name>PyRadiomics<", "<analysis:pipeline_version>3.1<", "<analysis:analysis_type>radiomics<",
                     "<analysis:run_status>SUCCEEDED<", "<analysis:auto_qc_status>NOT_EVALUATED<", "<analysis:container_id>900<",
                     f"<analysis:wrapup_version>proc-wrapup {__version__}<",
                     "<analysis:duration_seconds>120<", "<analysis:card_id>pyradiomics<", "<analysis:scans><analysis:scan>3<"):
        assert fragment in xml, fragment
    # the notes say where the tool's output is on the record (DERIVED, at its root), not the local
    # staging directory: "kept verbatim under raw/" was stale since DERIVED moved to the root
    notes = xml.split("<analysis:notes>")[1].split("</analysis:notes>")[0]
    assert "DERIVED" in notes and "raw/" not in notes, notes
    uploads = [c["path"].split("/out/resources/")[1].split("?")[0] for c in handler.calls if "/out/resources/" in c["path"]]
    assert "DERIVED/files/features.csv" in uploads and "REPORT/files/report.html" in uploads
    assert "PROVENANCE/files/wrapup.json" in uploads and "PROVENANCE/files/status.json" in uploads
    assert "PROVENANCE/files/card/metadata.json" in uploads and "PROVENANCE/files/card/card.json" in uploads
    assert manifest["card"]["card_id"] == "pyradiomics" and manifest["card"]["card_revision"] == "0.3.1"
    assert "LOGS/files/logs/stdout.log" in uploads and "DERIVED/files/sub/log.txt" in uploads
    assert not [u for u in uploads if u.startswith("METRICS/") or "/raw/" in u]     # METRICS is a view; DERIVED is the tree at its root
    assert manifest["analysis_record"]["id"] == "XNAT_E77777"
    assert manifest["views"] == manifest["analysis_record"]["views"] == {"METRICS": ["features.csv"]}
    assert "output_paths" not in manifest["analysis_record"]                     # local bookkeeping, not provenance
    logins = [c for c in handler.calls if c["path"] == "/data/JSESSION"]
    assert [c["method"] for c in logins] == ["POST", "DELETE"] and handler.calls[-1]["path"] == "/data/JSESSION"


def test_proc_wrapup_records_a_trapped_failure_as_failed_and_auto_qc_fail(cs, tmp_path, monkeypatch):
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 2, "workflow_id": "4990"})
    out = tmp_path / "out"
    set_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    xml = [c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"]][0]["body"].decode()
    assert "<analysis:run_status>FAILED<" in xml and "<analysis:auto_qc_status>FAIL<" in xml
    assert "FAILED" in (out / "report.html").read_text()


def test_proc_wrapup_without_status_json_finds_the_parent_by_the_shared_mount(cs, tmp_path, monkeypatch):
    """Codex P1 on PR #6: the workflow id before the wrapup's own (5000) belongs to a stranger's
    run (902) here; the parent is the container whose output mount is the wrapup's input mount."""
    host, handler = cs
    inp = tool_output(tmp_path)
    out = tmp_path / "out"
    set_env(monkeypatch, host)      # XNAT_WORKFLOW_ID=5001 -> own container 901 -> input mount BUILD -> parent 900
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["run_status"] == "SUCCEEDED" and manifest["execution"]["container_id"] == 900
    assert manifest["execution"]["docker_image"] == "radiomics/pyradiomics:CLI"
    assert (out / "logs" / "stderr.log").exists()


def test_find_parent_container_never_guesses(cs, caplog):
    """No status.json, and the wrapup's own container is missing or its mount is shared by two
    runs: no parent, no logs, rather than another run's logs on this record."""
    from segwrapup.execution import find_parent_container
    from segwrapup.register import XnatContext
    host, handler = cs
    context = XnatContext(host=host, user="u", password="p", project="P", session="S", scan="3")
    assert find_parent_container(context, None, "9999") is None          # own container unknown
    assert find_parent_container(context, None, None) is None
    twin = dict(CONTAINERS[2], id=903, mounts=[{"name": "output-mount", "writable": True, "xnat-host-path": BUILD}])
    handler.containers = CONTAINERS + [twin]
    with caplog.at_level(logging.WARNING):
        assert find_parent_container(context, None, "5001") is None       # two writers: ambiguous
    assert "ambiguous" in caplog.text


def test_proc_wrapup_survives_a_truncated_log_response(cs, tmp_path, monkeypatch, caplog):
    """Codex P1 on PR #6: an IncompleteRead from the logs endpoint must not abort the wrapup;
    raw/, the report and the record still ship, only that stream's log is missing."""
    host, handler = cs
    handler.truncate_logs = True
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    out = tmp_path / "out"
    set_env(monkeypatch, host)
    with caplog.at_level(logging.WARNING):
        assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    assert "could not fetch stdout of container 900" in caplog.text
    assert not (out / "logs" / "stdout.log").exists()
    assert (out / "raw" / "features.csv").exists() and (out / "report.html").exists()
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["execution"]["container_id"] == 900 and manifest["execution"]["logs"] == []
    assert manifest["analysis_record"]["id"] == "XNAT_E77777"


def test_no_publish_keeps_execution_capture(cs, tmp_path, monkeypatch):
    """Codex P2 on PR #6: --no-publish suppresses the record only; logs, container id and
    duration are still captured with the same one session, which is closed."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    out = tmp_path / "out"
    set_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out), "--no-publish"]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["analysis_record"] is None
    assert manifest["execution"]["container_id"] == 900 and manifest["execution"]["duration_seconds"] == 120
    assert (out / "logs" / "stdout.log").exists() and "line one" in (out / "report.html").read_text()
    assert not [c for c in handler.calls if c["method"] == "PUT"]
    assert [c["method"] for c in handler.calls if c["path"] == "/data/JSESSION"] == ["POST", "DELETE"]


def test_duration_uses_the_docker_running_and_complete_events():
    """demo02 container 35531: Created 17:38:54, running 17:39:00, complete 17:40:41,
    Finalizing/Complete 17:41:02. The run took 100 s, not the 128 s first-to-last."""
    from segwrapup.execution import _duration_seconds
    history = [("Created", "2026-09-06T17:38:54.553+0000"), ("running", "2026-09-06T17:39:00.926+0000"),
               ("complete", "2026-09-06T17:40:41.798+0000"), ("_Waiting", "2026-09-06T17:40:41.851+0000"),
               ("Finalizing", "2026-09-06T17:40:41.908+0000"), ("Complete", "2026-09-06T17:41:02.552+0000")]
    assert _duration_seconds({"history": [{"status": s, "time-recorded": t} for s, t in history]}) == 100
    assert _duration_seconds({"history": [{"status": s, "time-recorded": t} for s, t in history[:1]]}) is None
    assert _duration_seconds({"history": []}) is None


def test_proc_wrapup_without_contract_or_context_still_keeps_and_reports(cs, tmp_path, monkeypatch, caplog):
    host, handler = cs
    inp = tool_output(tmp_path)
    out = tmp_path / "out"
    for key in list(CONTRACT_ENV) + list(CONTEXT_ENV):
        monkeypatch.delenv(key, raising=False)
    with caplog.at_level(logging.INFO):
        assert proc.main(["--input", str(inp), "--output", str(out), "--pipeline", "MRtrix3"]) == 0
    assert (out / "raw" / "features.csv").exists() and (out / "report.html").exists()
    assert json.loads((out / "wrapup.json").read_text())["analysis_record"] is None
    assert handler.calls == [], "no XNAT context: nothing is requested"


def test_proc_wrapup_records_chain_and_prerequisites_and_can_leave_only_a_pointer(cs, tmp_path, monkeypatch):
    """0.5.0: the record names its orchestration step and the prerequisites record-fetch resolved;
    --pointer-only leaves wrapup.json alone for the output handler (the record owns the bytes)."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    (inp / "prereq.json").write_text(json.dumps({"prerequisites": [
        {"name": "qsiprep", "kind": "record", "path": "prereq/qsiprep", "files": 2, "role": "DERIVED",
         "record": {"ID": "XNAT_E12", "label": "qsiprep_S1_record", "pipeline_name": "qsiprep", "review_state": "ACCEPTED"}},
        {"name": "bids", "kind": "resource", "path": "prereq/bids", "files": 2, "resource": "BIDS"},
        {"name": "broken", "kind": "record", "error": "no record"}]}))
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "qsirecon"})
    assert proc.main(["--input", str(inp), "--output", str(out), "--pointer-only"]) == 0
    xml = [c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"]][0]["body"].decode()
    inputs = json.loads(xml.split("<analysis:inputs_json>")[1].split("</analysis:inputs_json>")[0].replace("&quot;", '"'))
    assert inputs["chain"] == {"orchestration_id": "42", "step": 2, "job_id": "job-abc", "workflow_id": "4990"}
    assert inputs["upstream_record"] == "XNAT_E12"
    assert [(q["name"], q["record"], q["resource"]) for q in inputs["prerequisites"]] == [("qsiprep", "XNAT_E12", None), ("bids", None, "BIDS")]
    # the files went to the record, then the output was reduced to the pointer
    uploads = [c["path"] for c in handler.calls if "/out/resources/" in c["path"]]
    assert any("DERIVED/files/features.csv" in u for u in uploads) and any("DERIVED/files/sub/log.txt" in u for u in uploads)
    assert sorted(p.name for p in out.rglob("*") if p.is_file()) == ["wrapup.json"]
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["chain"]["orchestration_id"] == "42" and manifest["analysis_record"]["id"] == "XNAT_E77777"


def test_proc_wrapup_without_orchestration_records_no_chain(cs, tmp_path, monkeypatch):
    host, handler = cs
    inp = tool_output(tmp_path)
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"XNAT_WORKFLOW_ID": "5002"})          # no workflow answer -> not orchestrated
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["chain"] is None and manifest["prerequisites"] == []
    assert (out / "raw" / "features.csv").exists()                     # no --pointer-only: output kept


def test_proc_wrapup_records_node_envelope_and_phase_timings_for_billing(cs, tmp_path, monkeypatch):
    """James, 2026-09-07: "accurate compute time … plus machine that ran it … for auditing and cost analysis"."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "pyradiomics"})
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    xml = [c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"]][0]["body"].decode()
    assert "<analysis:duration_seconds>120</analysis:duration_seconds>" in xml            # running -> complete of the main
    config = json.loads(xml.split("<analysis:config_json>")[1].split("</analysis:config_json>")[0].replace("&quot;", '"'))
    assert (config["backend"], config["node_id"], config["image"], config["user"]) == ("swarm", "laz2ephdgvajpbg96rhc6lfmn", "radiomics/pyradiomics:CLI", "admin")
    assert config["envelope"] == {"reserve_memory_mib": 256, "limit_memory_mib": 1024, "limit_cpu": 1.0, "generic_resources": {"GPU": "1"}, "swarm_constraints": []}
    main = config["phases"]["main"]
    assert (main["container_id"], main["seconds"], main["queue_wait_seconds"]) == (900, 120, 20)
    assert [ (p["container_id"], p["seconds"], p["timed_from"]) for p in config["phases"]["setup"] ] == [(899, 6, "running")]      # found by the shared mount
    assert main["timed_from"] == "running" and main["envelope"]["reserve_memory_mib"] == 256
    assert config["phases"]["setup"][0]["envelope"]["reserve_memory_mib"] == 1024                 # each phase carries its own envelope (Codex P2)
    wrapup = config["phases"]["wrapup"]
    assert wrapup["container_id"] == 901 and wrapup["seconds"] is None and wrapup["finished"] is None   # still running: not stamped as finished
    assert wrapup["in_progress"] is True and wrapup["elapsed_seconds_at_record"] >= 0
    assert config["total_seconds"] == 120 + 6                                                    # finished phases only
    assert "measured usage" in config["billing_note"] and "wrapup" in config["billing_note"]
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["execution"]["facts"]["node_id"] == "laz2ephdgvajpbg96rhc6lfmn"
    report = (out / "report.html").read_text()
    assert "laz2ephdgvajpbg96rhc6lfmn" in report and "swarm" in report


def test_pointer_only_a_derived_override_is_ignored_so_nothing_is_left_off_the_record(cs, tmp_path, monkeypatch, caplog):
    """Codex P1 on PR #10 made --pointer-only keep files an XNW_RESOURCE_DERIVED contract left off the
    record. Since 0.6.0 (plan D20) DERIVED is always the whole tool tree, the override is ignored and
    said so, everything is on the record, and the output reduces to the pointer. (A file that is
    still not on the record keeps staying in the output: the safety net is unchanged.)"""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "pyradiomics", "XNW_RESOURCE_DERIVED": "raw/features.csv"})
    with caplog.at_level(logging.WARNING):
        assert proc.main(["--input", str(inp), "--output", str(out), "--pointer-only"]) == 0
    uploads = [c["path"] for c in handler.calls if "/out/resources/" in c["path"]]
    assert any("DERIVED/files/sub/log.txt" in u for u in uploads) and any("DERIVED/files/features.csv" in u for u in uploads)
    assert [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()] == ["wrapup.json"]
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["ignored_overrides"] == ["DERIVED=raw/features.csv"]
    assert "DERIVED is a fixed resource" in caplog.text and "stay in the output" not in caplog.text


def test_a_status_json_and_prereq_json_in_the_tool_output_are_provenance_not_part_of_the_dataset(cs, tmp_path, monkeypatch):
    """The card's exit trap writes status.json into the tool's /output and the command line copies
    prereq.json there; on 0.5.0 both landed in the dataset (mriqc E25614: METRICS raw/status.json).
    James: "we have a predefined dataset that's created by the scientists. Don't fuck it up."."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    (inp / "prereq.json").write_text(json.dumps({"prerequisites": [{"name": "conv", "kind": "record", "path": "prereq/conv",
                                                                     "files": 1, "role": "PROVENANCE", "record": {"ID": "XNAT_E5"}}]}))
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "mriqc"})
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    uploads = [c["path"].split("/out/resources/")[1].split("?")[0] for c in handler.calls if "/out/resources/" in c["path"]]
    assert "PROVENANCE/files/status.json" in uploads and "PROVENANCE/files/prereq.json" in uploads
    assert "DERIVED/files/status.json" not in uploads and "DERIVED/files/prereq.json" not in uploads
    assert not (out / "raw" / "status.json").exists() and not (out / "raw" / "prereq.json").exists()
    assert (out / "status.json").read_bytes() == (inp / "status.json").read_bytes()          # verbatim, not re-serialised
    assert sorted(u for u in uploads if u.startswith("DERIVED/")) == ["DERIVED/files/features.csv", "DERIVED/files/sub/log.txt"]
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["prerequisites"][0]["record"]["ID"] == "XNAT_E5"                          # still read for the record's inputs


def test_proc_wrapup_publishes_the_datasets_dotfiles_in_derived_and_never_the_dicom_copy(cs, tmp_path, monkeypatch):
    """The QSIRECON shape (demo02 XNAT_E09349 carries a .bidsignore): a dotfile at the dataset root and
    one in a subdirectory land in DERIVED at their paths; the card's .source_dicom is uploaded nowhere."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    (inp / ".bidsignore").write_text("*.html\n")
    (inp / "sub" / ".state").write_text("done")
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "qsirecon"})
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    uploads = sorted(c["path"].split("/out/resources/")[1].split("?")[0] for c in handler.calls if "/out/resources/" in c["path"])
    assert [u for u in uploads if u.startswith("DERIVED/")] == [
        "DERIVED/files/.bidsignore", "DERIVED/files/features.csv", "DERIVED/files/sub/.state", "DERIVED/files/sub/log.txt"]
    assert not [c["path"] for c in handler.calls if ".source_dicom" in c["path"]]
    assert not (out / "raw" / ".source_dicom").exists()
    manifest = json.loads((out / "wrapup.json").read_text())
    assert "raw/.bidsignore" in manifest["raw_files"] and "raw/sub/.state" in manifest["raw_files"]
    assert "PROVENANCE/files/status.json" in uploads and "DERIVED/files/status.json" not in uploads


def test_proc_wrapup_publishes_a_bids_derivatives_dataset_at_the_derived_root_with_views_and_links_the_tool_report(cs, tmp_path, monkeypatch, caplog):
    """The fmriprep/mriqc shape of XNAT_E25617/E25614 on a card still carrying 0.5.0 globs: the whole
    dataset is DERIVED with no raw/ segment, the tool's HTML report stays beside its figures and is
    linked from report.html, METRICS is a view, and the card's REPORT/PROVENANCE globs are ignored."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    (inp / "sub-H025" / "figures").mkdir(parents=True); (inp / "sub-H025" / "anat").mkdir()
    (inp / "dataset_description.json").write_text('{"Name": "fMRIPrep"}')
    (inp / "sub-H025.html").write_text('<img src="sub-H025/figures/a.svg">')
    (inp / "sub-H025" / "figures" / "a.svg").write_text("<svg/>")
    (inp / "sub-H025" / "anat" / "sub-H025_T1w.json").write_text('{"cjv": 0.4}')
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "fmriprep", "XNW_RESOURCE_METRICS": "raw/sub-*/**/*.json",
                                "XNW_RESOURCE_REPORT": "report.html,raw/sub-*.html",
                                "XNW_RESOURCE_PROVENANCE": "wrapup.json,status.json,raw/prereq.json"})
    with caplog.at_level(logging.WARNING):
        assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    uploads = sorted(c["path"].split("/out/resources/")[1].split("?")[0] for c in handler.calls if "/out/resources/" in c["path"])
    assert [u for u in uploads if u.startswith("DERIVED/")] == [
        "DERIVED/files/dataset_description.json", "DERIVED/files/features.csv", "DERIVED/files/sub-H025.html",
        "DERIVED/files/sub-H025/anat/sub-H025_T1w.json", "DERIVED/files/sub-H025/figures/a.svg", "DERIVED/files/sub/log.txt"]
    assert [u for u in uploads if u.startswith("REPORT/")] == ["REPORT/files/report.html"]
    assert not [u for u in uploads if u.startswith("METRICS/") or "raw/" in u]
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["views"] == {"METRICS": ["sub-H025/anat/sub-H025_T1w.json"]}
    assert manifest["ignored_overrides"] == ["REPORT=report.html,raw/sub-*.html", "PROVENANCE=wrapup.json,status.json,raw/prereq.json"]
    assert manifest["derived_root"] == "raw"
    report = (out / "report.html").read_text()
    assert '<a href="../../DERIVED/files/sub-H025.html"' in report          # resolved against the record page's <base> at REPORT/files/
    assert "drop the raw/ prefix" in caplog.text and "REPORT is a fixed resource" in caplog.text


def test_pointer_only_removes_the_empty_files_the_record_could_not_take(cs, tmp_path, monkeypatch):
    """demo02 2026-09-08: proc-wrapup 0.5.0 skipped the zero-byte files at publish but left them in the
    output, so the session's XNW_BIDS pointer resource carried an empty stderr.log and XNW_FMRIPREP an
    empty stderr.log and patchdir.txt. A skipped file is uploaded nowhere."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    (inp / "patchdir.txt").write_bytes(b"")
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "fmriprep"})
    assert proc.main(["--input", str(inp), "--output", str(out), "--pointer-only"]) == 0
    assert not [c for c in handler.calls if "patchdir.txt" in c["path"]]
    assert [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()] == ["wrapup.json"]
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["analysis_record"]["skipped_empty"] == ["patchdir.txt"]


def test_phase_without_a_running_event_is_timed_from_created():
    """demo02 2026-09-07: setup containers that finish in seconds have no 'running' entry in the CS history."""
    from segwrapup.execution import _phase
    phase = _phase({"id": 7, "history": [{"status": "Created", "time-recorded": "2026-09-07T18:40:25.832+0000"},
                                         {"status": "complete", "time-recorded": "2026-09-07T18:40:34.665+0000"},
                                         {"status": "Complete", "time-recorded": "2026-09-07T18:40:35.107+0000"}]})
    assert (phase["seconds"], phase["timed_from"]) == (8, "created") and "queue_wait_seconds" not in phase
    assert _phase({"id": 8, "history": []})["seconds"] is None


def test_pointer_only_exempts_only_the_root_manifest(cs, tmp_path, monkeypatch):
    """Codex P2 on PR #10: a nested wrapup.json from a previous run is data, not the pointer."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    (inp / "previous").mkdir(); (inp / "previous" / "wrapup.json").write_text('{"old": true}')
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "pyradiomics"})
    assert proc.main(["--input", str(inp), "--output", str(out), "--pointer-only"]) == 0
    assert [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()] == ["wrapup.json"]
    assert any("DERIVED/files/previous/wrapup.json" in c["path"] for c in handler.calls)   # it went to the record, as the tool laid it out


def test_a_subject_scoped_run_publishes_a_subject_record_naming_the_sessions_it_spanned(cs, tmp_path, monkeypatch):
    """A subject-context wrapper sets PROC_SUBJECT_ID and no PROC_SESSION_ID (0.6.2): the record
    is an analysis:subjectAnalysisData under the subject, its files are experiment resources,
    and inputs_json lists the subject's sessions, which the setup assembled into one tree."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "fmriprep", "PROC_PIPELINE_VERSION": "25.2.5", "PROC_SUBJECT_ID": "XNAT_S09007"})
    monkeypatch.delenv("PROC_SESSION_ID"); monkeypatch.delenv("PROC_SCAN_ID")
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    creates = [c for c in handler.calls if c["method"] == "PUT" and "/subjects/" in c["path"] and "/resources/" not in c["path"]]
    assert len(creates) == 1 and creates[0]["path"].startswith("/data/projects/PROJ_1/subjects/XNAT_S09007/experiments/fmriprep_292_")
    xml = creates[0]["body"].decode()
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<analysis:SubjectAnalysis ')
    assert "<xnat:subject_ID>XNAT_S09007</xnat:subject_ID>" in xml and "imageSession_ID" not in xml and "<analysis:scans>" not in xml
    inputs = json.loads(html.unescape(xml.split("<analysis:inputs_json>")[1].split("</analysis:inputs_json>")[0]))
    assert inputs["scope"] == "subject" and inputs["subject"] == "XNAT_S09007"
    assert inputs["sessions"] == [{"ID": "XNAT_E25641", "label": "292_postop"}, {"ID": "XNAT_E25642", "label": "292_preop"}]
    uploads = [c["path"] for c in handler.calls if c["method"] == "PUT" and "/resources/" in c["path"]]
    assert uploads and all(u.startswith("/data/experiments/XNAT_E77777/resources/") for u in uploads)
    assert not [c for c in handler.calls if "/assessors/" in c["path"] or "/out/" in c["path"]]
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["scope"] == "subject" and [s["label"] for s in manifest["sessions"]] == ["292_postop", "292_preop"]
    assert manifest["analysis_record"]["xsi_type"] == "analysis:subjectAnalysisData"
    assert "292_postop, 292_preop" in (out / "report.html").read_text()


def test_a_dataset_scoped_run_publishes_a_group_record_citing_the_dataset(cs, tmp_path, monkeypatch):
    """A dataset-context wrapper sets PROC_DATASET_ID and neither a session nor a subject
    (0.7.0): the record is an analysis:groupAnalysisData project asset of the project, created
    by label under the project, its files experiment resources; input_dataset_id cites the
    frozen dataset, subject_count is the cohort's included_count, and inputs_json names the
    dataset's label so a reader knows the cohort without opening the tool's output."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "fitlins", "PROC_PIPELINE_VERSION": "0.11.0", "PROC_DATASET_ID": "XNAT_D0001"})
    monkeypatch.delenv("PROC_SESSION_ID"); monkeypatch.delenv("PROC_SCAN_ID")
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    creates = [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/")]
    assert len(creates) == 1 and creates[0]["path"].startswith("/data/projects/PROJ_1/experiments/fitlins_flanker-2sub_")
    assert creates[0]["path"].endswith("_record?inbody=true")
    xml = creates[0]["body"].decode()
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<analysis:GroupAnalysis ')
    assert "subject_ID" not in xml and "imageSession_ID" not in xml and "<analysis:scans>" not in xml
    assert "<analysis:input_dataset_id>XNAT_D0001</analysis:input_dataset_id>" in xml and "<analysis:subject_count>2</analysis:subject_count>" in xml
    inputs = json.loads(html.unescape(xml.split("<analysis:inputs_json>")[1].split("</analysis:inputs_json>")[0]))
    assert inputs["scope"] == "dataset" and inputs["dataset"] == "XNAT_D0001" and inputs["dataset_label"] == "flanker-2sub"
    assert inputs["included_count"] == 2 and inputs["project"] == "PROJ_1" and inputs["sessions"] == []
    uploads = [c["path"] for c in handler.calls if c["method"] == "PUT" and "/resources/" in c["path"]]
    assert uploads and all(u.startswith("/data/experiments/XNAT_E77777/resources/") for u in uploads)
    assert not [c for c in handler.calls if "/assessors/" in c["path"] or "/out/" in c["path"] or "/subjects/" in c["path"]]
    # the asset was read once (proc-wrapup's own facts; publish_if_possible did not ask again)
    assert [c["path"] for c in handler.calls if c["path"].startswith("/data/experiments/XNAT_D0001")] == ["/data/experiments/XNAT_D0001?format=json"]
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["scope"] == "dataset" and manifest["dataset"] == {"id": "XNAT_D0001", "label": "flanker-2sub", "included_count": 2}
    assert manifest["analysis_record"]["xsi_type"] == "analysis:groupAnalysisData" and manifest["card"]["run_scope"] == "dataset"
    assert "flanker-2sub (XNAT_D0001), 2 included" in (out / "report.html").read_text()


# ── training cards: produces=model (0.7.1) ────────────────────────────────────

def _training_output(tmp_path, with_card=True):
    inp = tmp_path / "in"
    inp.mkdir(parents=True)
    (inp / "segmentation_spleen.pt").write_bytes(b"WEIGHTS")
    (inp / "train_stats.json").write_text(json.dumps({"best_metric": 0.91}))
    if with_card:
        (inp / "model-card.json").write_text(json.dumps({"model_framework": "monailabel", "model": "segmentation_spleen",
                                                        "default_checkpoint": "segmentation_spleen.pt", "task_type": "segmentation",
                                                        "labels": {"spleen": 1}, "train_cases": 3, "train_stats": {"best_metric": 0.91}}))
    (inp / "status.json").write_text(json.dumps({"exit_code": 0, "workflow_id": "4990"}))
    return inp


def _training_env(monkeypatch, host, extra=None):
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "monailabel-train", "PROC_PIPELINE_VERSION": "0.1.0", "PROC_DATASET_ID": "XNAT_D0001",
                                "XNW_PRODUCES": "model", "XNW_RESOURCE_MODEL": "*.pt,model-card.json", "XNW_RESOURCE_METRICS": "train_stats.json",
                                **(extra or {})})
    monkeypatch.delenv("PROC_SESSION_ID"); monkeypatch.delenv("PROC_SCAN_ID")


def manifest_label(out):
    return json.loads((out / "wrapup.json").read_text())["trained_model"]["label"]


def test_a_training_card_registers_a_draft_model_and_links_the_run(cs, tmp_path, monkeypatch):
    """produces=model: after the group record, the MODEL view's weights become an
    analysis:trainedModelData project asset in DRAFT (weights on MODEL, the tool's model card on
    MODEL_CARD, provenance on PROVENANCE), source_dataset_id points back (source_training_id is a foreign
    key to groupTrainingData in schema plugin 0.2.0 and would 500, as it did live on demo02 2026-09-26; the
    run id rides in engine_metadata_json.source_run_id), and
    the run record gets results_json.trained_model (produced_model_id belongs to groupTrainingData
    only in schema plugin 0.2.0; the query-parameter update created stray records live), so the link pair of DATASET-SCOPE-CARDS-DESIGN §7.2
    exists without the retired group-analysis-wrapup."""
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    _training_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    creates = [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    assert [c["path"].split("/")[5].split("_")[0] for c in creates] == ["monailabel-train", "model"], "the run record first, then the model"
    model_xml = creates[1]["body"].decode()
    assert model_xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<analysis:TrainedModel ') and 'project="PROJ_1" label="model_flanker-2sub_' in model_xml
    for fragment in ("<analysis:model_status>DRAFT</analysis:model_status>",                      "<analysis:source_dataset_id>XNAT_D0001</analysis:source_dataset_id>", "<analysis:model_framework>monailabel</analysis:model_framework>",
                     "<analysis:default_checkpoint>segmentation_spleen.pt</analysis:default_checkpoint>", "<analysis:model_name>segmentation_spleen</analysis:model_name>",
                     "<analysis:label_names>spleen:1</analysis:label_names>", "<analysis:num_classes>2</analysis:num_classes>",
                     "<analysis:best_validation_dice>0.91</analysis:best_validation_dice>", "<analysis:model_resource_label>MODEL</analysis:model_resource_label>",
                     "<analysis:created_by>jdickson</analysis:created_by>"):
        assert fragment in model_xml, fragment
    assert "source_training_id" not in model_xml, "a foreign key to analysis:groupTrainingData; the run is a groupAnalysisData"
    import re, html
    meta = json.loads(html.unescape(re.search(r"<analysis:engine_metadata_json>(.*?)</analysis:engine_metadata_json>", model_xml, re.S).group(1)))
    assert meta["source_run_id"] == "XNAT_E77777" and meta["source_run_type"] == "analysis:groupAnalysisData"
    uploads = [c["path"].split("?")[0] for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/experiments/XNAT_E88888/resources/")]
    assert uploads == ["/data/experiments/XNAT_E88888/resources/MODEL/files/segmentation_spleen.pt",
                       "/data/experiments/XNAT_E88888/resources/MODEL_CARD/files/model-card.json",
                       "/data/experiments/XNAT_E88888/resources/PROVENANCE/files/provenance.json"]
    links = [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/experiments/XNAT_E77777?")]
    assert len(links) == 1 and links[0]["path"] == "/data/experiments/XNAT_E77777?xsiType=analysis%3AgroupAnalysisData"
    link_xml = links[0]["body"].decode()
    assert 'ID="XNAT_E77777" project="PROJ_1" label="run_label_x"' in link_xml and "produced_model_id" not in link_xml
    merged = json.loads(html.unescape(re.search(r"<analysis:results_json>(.*?)</analysis:results_json>", link_xml, re.S).group(1)))
    assert merged["trained_model"] == {"id": "XNAT_E88888", "label": manifest_label(out), "xsi_type": "analysis:trainedModelData", "status": "DRAFT"}
    assert merged["views"] == {"MODEL": ["segmentation_spleen.pt"]} and merged["model"] == "monailabel-train", "what the publish wrote is kept"
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["analysis_record"]["produces"] == "model" and sorted(manifest["analysis_record"]["views"]["MODEL"]) == ["model-card.json", "segmentation_spleen.pt"]
    assert manifest["trained_model"] == {"xsi_type": "analysis:trainedModelData", "id": "XNAT_E88888", "label": manifest["trained_model"]["label"],
                                         "status": "DRAFT", "weights": ["segmentation_spleen.pt"], "model_card": True,
                                         "source_run_id": "XNAT_E77777", "linked": True}
    # the weights are still on the run record's DERIVED, untouched: the model asset is a second home, not a move
    assert "/data/experiments/XNAT_E77777/resources/DERIVED/files/segmentation_spleen.pt" in [c["path"].split("?")[0] for c in handler.calls]


def test_produces_model_without_weights_keeps_the_run_record_and_says_so(cs, tmp_path, monkeypatch):
    host, handler = cs
    inp, out = _training_output(tmp_path, with_card=False), tmp_path / "out"
    os.remove(inp / "segmentation_spleen.pt")
    _training_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    creates = [c["path"] for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    assert len(creates) == 1 and "/experiments/model_" not in creates[0], "no model asset without weights; the run record stands"
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["analysis_record"]["id"] == "XNAT_E77777" and manifest["trained_model"] == {"error": "no MODEL view files on DERIVED"}
    assert not [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/experiments/XNAT_E77777?")]


def test_produces_model_on_a_failed_run_registers_nothing(cs, tmp_path, monkeypatch):
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    (inp / "status.json").write_text(json.dumps({"exit_code": 1, "workflow_id": "4990"}))
    _training_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["run_status"] == "FAILED" and manifest["trained_model"] == {"skipped": "run FAILED"}
    assert not [c for c in handler.calls if "/experiments/model_" in c["path"] or (c["method"] == "PUT" and c["path"].startswith("/data/experiments/XNAT_E77777?"))]


def test_a_model_registration_that_fails_leaves_the_run_record_and_records_the_error(cs, tmp_path, monkeypatch):
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    _training_env(monkeypatch, host)
    from segwrapup import model as model_module
    monkeypatch.setattr(model_module, "publish_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PUT failed: HTTP 500 boom")))
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["analysis_record"]["id"] == "XNAT_E77777"
    assert manifest["trained_model"]["error"] == "PUT failed: HTTP 500 boom" and manifest["trained_model"]["label"].startswith("model_flanker-2sub_")
    assert not [c for c in handler.calls if c["method"] == "DELETE" and "/data/experiments/" in c["path"]], "the run record is never rolled back for the model's sake"


def test_a_session_scoped_run_never_registers_a_model(cs, tmp_path, monkeypatch):
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    set_env(monkeypatch, host, {"XNW_PRODUCES": "model", "XNW_RESOURCE_MODEL": "*.pt"})
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["trained_model"]["error"].startswith("produces=model is only defined at dataset scope")
    assert not [c for c in handler.calls if "/experiments/model_" in c["path"]]


def test_nested_model_weights_keep_their_view_relative_names(cs, tmp_path, monkeypatch):
    """fold-0/best.pt and fold-1/best.pt are two checkpoints; uploading both as MODEL/files/best.pt
    would let the second overwrite the first, and default_checkpoint would name a path that is not
    on the resource (Codex P1, PR #21)."""
    host, handler = cs
    inp = _training_output(tmp_path)
    (inp / "segmentation_spleen.pt").unlink()
    for fold in ("fold-0", "fold-1"):
        (inp / fold).mkdir()
        (inp / fold / "best.pt").write_bytes(b"w" + fold.encode())
    card = json.loads((inp / "model-card.json").read_text()); card["default_checkpoint"] = "fold-0/best.pt"
    (inp / "model-card.json").write_text(json.dumps(card))
    _training_env(monkeypatch, host, {"XNW_RESOURCE_MODEL": "**/*.pt,model-card.json"})
    assert proc.main(["--input", str(inp), "--output", str(tmp_path / "out")]) == 0
    uploads = sorted(c["path"].split("?")[0] for c in handler.calls if c["method"] == "PUT" and "/experiments/XNAT_E88888/resources/MODEL/" in c["path"])
    assert uploads == ["/data/experiments/XNAT_E88888/resources/MODEL/files/fold-0/best.pt",
                       "/data/experiments/XNAT_E88888/resources/MODEL/files/fold-1/best.pt"]
    manifest = json.loads((tmp_path / "out" / "wrapup.json").read_text())
    assert manifest["trained_model"]["weights"] == ["fold-0/best.pt", "fold-1/best.pt"]
    model_xml = next(c["body"].decode() for c in handler.calls if c["method"] == "PUT" and "/experiments/model_" in c["path"])
    assert "<analysis:default_checkpoint>fold-0/best.pt</analysis:default_checkpoint>" in model_xml


def test_default_checkpoint_must_be_an_uploaded_weight():
    """A stale card value would send consumers to a file the MODEL resource does not hold (Codex P2, PR #21)."""
    from segwrapup.model import default_checkpoint
    assert default_checkpoint("best.pt", ["best.pt"]) == "best.pt"
    assert default_checkpoint("fold-0/best.pt", ["fold-0/best.pt", "fold-1/best.pt"]) == "fold-0/best.pt"
    assert default_checkpoint("best.pt", ["fold-0/best.pt"]) == "fold-0/best.pt", "by basename when that is unambiguous"
    assert default_checkpoint("best.ckpt", ["best.pt"]) == "best.pt", "the only weight wins over a stale name"
    assert default_checkpoint("best.ckpt", ["fold-0/best.pt", "fold-1/best.pt"]) is None, "ambiguous: registered without one"
    assert default_checkpoint(None, ["a.pt", "b.pt"]) is None and default_checkpoint("", ["only.pt"]) == "only.pt"
    # a non-string card value is undeclared, not an AttributeError that fails the wrapup after the run was published (Codex P2, round 4)
    assert default_checkpoint(123, ["only.pt"]) == "only.pt" and default_checkpoint({"path": "x"}, ["a.pt", "b.pt"]) is None
    assert default_checkpoint(["best.pt"], ["best.pt"]) == "best.pt", "a list is not a string either; the only weight wins"

