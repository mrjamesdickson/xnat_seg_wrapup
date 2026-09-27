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


def test_a_generated_model_label_names_its_run_so_two_runs_in_one_second_do_not_collide():
    """Two training cards finishing on the same dataset within a second produced the same
    model_<dataset>_<stamp>; the second hit the create-only preflight (200) and lost its model
    (Codex P2, PR #21 round 4). The run id is unique per run."""
    from datetime import datetime, timezone
    from segwrapup.model import model_label
    when = datetime(2026, 9, 26, 22, 15, 38, tzinfo=timezone.utc)
    a = model_label("nnunet-nnunet_msd_spleen_demo-20260803_121930", when, run_id="XNAT_E26048")
    b = model_label("nnunet-nnunet_msd_spleen_demo-20260803_121930", when, run_id="XNAT_E26050")
    assert a != b and a.endswith("_20260926T221538Z_E26048") and b.endswith("_20260926T221538Z_E26050")
    assert len(a) <= 64 and a.startswith("model_nnunet-nnunet_msd_spleen_demo"), "hyphens are label-safe; the head is trimmed for the tail"
    assert model_label("ds", when) == "model_ds_20260926T221538Z", "no run id: the old shape"
    # the run id may be a 64-character fallback label (no id in XNAT's answer): the tail is bounded and stays unique (round 8)
    long_run = "monailabel-train_flanker-2sub_20260926T221538Z_" + "x" * 20
    bounded = model_label("nnunet-nnunet_msd_spleen_demo-20260803_121930", when, run_id=long_run)
    assert len(bounded) <= 64 and bounded.startswith("model_nnunet") and "_20260926T221538Z_" in bounded
    assert bounded != model_label("nnunet-nnunet_msd_spleen_demo-20260803_121930", when, run_id=long_run[:-1] + "y")
    assert model_label("ds", when, run_id="") == "model_ds_20260926T221538Z"
    # only an accession id loses its site prefix: two pipelines' fallback labels on one dataset in one
    # second differ only before the first underscore, and must give two model labels (round 11)
    a = model_label("ds", when, run_id="trainerA_ds_20260926T221538Z_record")
    b = model_label("ds", when, run_id="trainerB_ds_20260926T221538Z_record")
    assert a != b and "trainerA" in a and "trainerB" in b and len(a) <= 64
    assert model_label("ds", when, run_id="CENTRAL_E7").endswith("_E7") and model_label("ds", when, run_id="XNAT_E26051").endswith("_E26051")
    assert model_label("ds", when, run_id="XNAT_E26051x").endswith("_XNAT_E26051x"), "not an accession id: kept whole"
    # a site prefix with _, - or . is still an accession id (round 18)
    from segwrapup.model import _ACCESSION_ID
    for site_id in ("MY_SITE_E123", "my-site.v2_E7", "A_E1"):
        assert _ACCESSION_ID.match(site_id), site_id
    assert model_label("ds", when, run_id="MY_SITE_E123").endswith("_E123") and model_label("ds", when, run_id="my-site.v2_E7").endswith("_E7")
    assert not _ACCESSION_ID.match("_E1") and not _ACCESSION_ID.match("trainerA_ds_20260926T221538Z_record")


def test_num_classes_comes_from_the_label_indices_not_the_number_of_names():
    """{"background": 0, "spleen": 1} is a two-class network, not three (Codex P2, PR #21 round 5)."""
    from segwrapup.model import num_classes
    assert num_classes({"background": 0, "spleen": 1}) == 2 and num_classes({"spleen": 1}) == 2 and num_classes({"foreground": 1}) == 2
    assert num_classes({"spleen": 1, "liver": 2}) == 3 and num_classes({"a": 1, "c": 3}) == 4, "sparse indices count by the highest"
    assert num_classes({}) is None and num_classes({"spleen": "one"}) is None and num_classes({"x": True}) is None
    # a JSON number out of range decodes as infinity; NaN and fractions are not indices either (Codex P2, round 7)
    assert num_classes({"lesion": float("inf"), "spleen": 1}) == 2 and num_classes({"lesion": float("nan")}) is None
    assert num_classes({"half": 1.5, "two": 2.0}) == 3 and num_classes({"neg": -1}) is None
    assert num_classes({"huge": 10 ** 400, "spleen": 1}) == 2, "a 400-digit integer is not an index and must not overflow"
    from segwrapup.model import _best_dice
    assert _best_dice({"best_metric": 10 ** 400}) is None and _best_dice({"best_metric": 1}) == 1.0 and _best_dice({"best_metric": True}) is None
    assert _best_dice({"train_stats": {"best_metric": float("nan")}, "best_validation_dice": 0.7}) == 0.7 and _best_dice({"best_metric": float("nan")}) is None, "NaN is no dice"
    assert _best_dice({"train_stats": {"best_metric": 0.91}}) == 0.91
    # a finite value outside [0, 1] is a loss or a count under the generic key, not a Dice (round 11)
    assert _best_dice({"best_metric": 12}) is None and _best_dice({"best_metric": -0.5}) is None and _best_dice({"best_metric": 1.0000001}) is None
    assert _best_dice({"best_validation_dice": 7, "best_metric": 0.5}) == 0.5, "the out-of-range key is skipped, the next one read"
    assert _best_dice({"best_metric": 0.0}) == 0.0 and _best_dice({"best_metric": 1}) == 1.0
    assert num_classes({"top": 65535}) == 65536 and num_classes({"over": 65536}) is None


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


def test_a_missing_path_qualified_default_checkpoint_is_not_remapped_to_another_fold():
    """`fold-0/best.pt` declared, only `fold-1/best.pt` uploaded: a basename match or the only-weight
    fallback would make every consumer load the other fold under the card's name (Codex P2, round 22).
    A bare basename keeps its basename match; an exact path match is still honoured."""
    from segwrapup.model import default_checkpoint
    assert default_checkpoint("fold-0/best.pt", ["fold-1/best.pt"]) is None, "the only weight is a different fold"
    assert default_checkpoint("fold-0/best.pt", ["fold-1/best.pt", "fold-1/final.pt"]) is None
    assert default_checkpoint("fold-0/best.pt", ["fold-0/best.pt"]) == "fold-0/best.pt"
    assert default_checkpoint("best.pt", ["fold-1/best.pt"]) == "fold-1/best.pt", "a bare basename still matches by basename"
    assert default_checkpoint("models/best.pt", ["best.pt"]) is None, "a path is not shortened to its basename either"
    assert default_checkpoint("fold-0\\best.pt", ["fold-1/best.pt"]) is None, "a Windows-style path is a path, not a basename"
    assert default_checkpoint("fold-0\\best.pt", ["fold-0/best.pt", "fold-1/best.pt"]) == "fold-0/best.pt", "and names the POSIX path it was uploaded as (round 25)"
    assert default_checkpoint("fold-0\\best.pt", ["fold-0\\best.pt"]) == "fold-0\\best.pt", "an exact match is still honoured"
    assert default_checkpoint("models\\best.pt", ["best.pt"]) is None



def test_engine_metadata_is_bounded_to_the_schema_cap(caplog):
    """A detailed model card must not make XNAT refuse the trained-model record (Codex P2, PR #21 round 10):
    the train_stats lists go first, then train_stats, then everything but the run link."""
    import logging
    from segwrapup.model import ENGINE_METADATA_MAX, bounded_engine_metadata
    link = {"source_run_id": "XNAT_E1", "source_run_type": "analysis:groupAnalysisData"}
    small = {"app": "radiology", "train_stats": {"best_metric": 0.8, "epochs": 5, "history": [0.1, 0.5, 0.8]}, **link}
    assert json.loads(bounded_engine_metadata(small)) == small, "under the cap: verbatim"
    history = [{"epoch": i, "loss": 0.5, "dice": 0.7} for i in range(3000)]
    detailed = {"app": "radiology", "max_epochs": 3000, "train_stats": {"best_metric": 0.8, "best_epoch": 2999, "history": history,
                                                                        "per_class": {"spleen": history}}, **link}
    assert len(json.dumps(detailed)) > ENGINE_METADATA_MAX
    with caplog.at_level(logging.WARNING):
        reduced = json.loads(bounded_engine_metadata(detailed))
    assert reduced["train_stats"] == {"best_metric": 0.8, "best_epoch": 2999} and reduced["truncated"] == ["train_stats"]
    assert reduced["app"] == "radiology" and reduced["source_run_id"] == "XNAT_E1"
    assert len(json.dumps(reduced)) <= ENGINE_METADATA_MAX and "reduced to its scalars" in caplog.text
    # scalars alone over the cap: train_stats goes entirely
    wide = {**detailed, "train_stats": {f"metric_{i}": i for i in range(9000)}}
    reduced = json.loads(bounded_engine_metadata(wide))
    assert "train_stats" not in reduced and reduced["truncated"] == ["train_stats"] and reduced["app"] == "radiology"
    # the rest over the cap too: only the link survives, and the record says what it lost
    huge = {"app": "x" * 70000, "base_model": "b", **link}
    reduced = json.loads(bounded_engine_metadata(huge))
    assert reduced == {**link, "truncated": ["app", "base_model"]}
    assert len(json.dumps(reduced)) <= ENGINE_METADATA_MAX
    # a number that overflowed to infinity, or a NaN, is never written as Infinity/NaN (round 19)
    text = bounded_engine_metadata({"train_stats": {"best_metric": float("inf"), "loss": [0.5, float("nan")]}, "val_split": 0.2, **link})
    assert "Infinity" not in text and "NaN" not in text
    strict = json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert strict["train_stats"] == {"best_metric": None, "loss": [0.5, None]} and strict["non_finite_values_replaced"] == 2 and strict["val_split"] == 0.2
    # nested past the recursion limit: the run link survives, the rest is named as dropped (round 20)
    deep = {}
    for _ in range(3000):
        deep = {"n": deep}
    text = bounded_engine_metadata({"app": "radiology", "train_stats": deep, **link})
    assert json.loads(text) == {**link, "truncated": ["app", "train_stats"]}


def test_an_empty_model_card_is_still_uploaded_and_named(cs, tmp_path, monkeypatch):
    """`{}` is a valid card with no metadata: the file the MODEL view selected goes on MODEL_CARD and
    the record names the resource; only the metadata-derived fields are absent (Codex P2, PR #21 round 11)."""
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    (inp / "model-card.json").write_text("{}")
    _training_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    creates = [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    model_xml = creates[1]["body"].decode()
    assert "<analysis:model_card_resource_label>MODEL_CARD</analysis:model_card_resource_label>" in model_xml
    assert "<analysis:label_names>" not in model_xml and "<analysis:best_validation_dice>" not in model_xml
    uploads = [c["path"].split("?")[0] for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/experiments/XNAT_E88888/resources/")]
    assert "/data/experiments/XNAT_E88888/resources/MODEL_CARD/files/model-card.json" in uploads
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["trained_model"]["model_card"] is True
    # no card file at all: no MODEL_CARD resource, and the record does not name one
    from segwrapup.model import model_card_path, read_model_card
    bare = tmp_path / "bare"; bare.mkdir(); (bare / "segmentation_spleen.pt").write_bytes(b"w")
    assert model_card_path(bare, ["segmentation_spleen.pt"]) is None and read_model_card(bare, ["segmentation_spleen.pt"]) == {}
    assert model_card_path(inp, ["segmentation_spleen.pt"]) == inp / "model-card.json", "the DERIVED-root fallback"
    assert model_card_path(inp, ["model-card.json"]) == inp / "model-card.json"
    (inp / "model-card.json").write_text("not json")
    assert read_model_card(inp, ["model-card.json"]) == {} and model_card_path(inp, ["model-card.json"]) is not None, "unreadable: no metadata, but the file is there"
    # syntactically valid but nested past the parser's recursion limit: no metadata, the file stays (round 24)
    (inp / "model-card.json").write_text('{"train_stats": ' + "[" * 100000 + "]" * 100000 + "}")
    assert read_model_card(inp, ["model-card.json"]) == {} and model_card_path(inp, ["model-card.json"]) is not None
    (inp / "model-card.json").write_bytes(b"")
    assert model_card_path(inp, ["model-card.json"]) is None, "zero bytes: XNAT would refuse the upload, so no card is advertised (round 12)"
    # only the exact basename is the card: a look-alike in a broad view is not (round 15)
    from segwrapup.model import is_model_card
    (bare / "backup-model-card.json").write_text(json.dumps({"model": "old"}))
    assert not is_model_card("backup-model-card.json") and is_model_card("fold-0/model-card.json")
    assert model_card_path(bare, ["backup-model-card.json", "segmentation_spleen.pt"]) is None
    assert read_model_card(bare, ["backup-model-card.json"]) == {}


def test_a_look_alike_card_name_in_the_model_view_is_a_weight_file_not_the_card(cs, tmp_path, monkeypatch):
    """`backup-model-card.json` sorts before `model-card.json` in a broad MODEL view; it must neither
    supply the metadata nor be uploaded under the canonical name (Codex P2, PR #21 round 15)."""
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    (inp / "backup-model-card.json").write_text(json.dumps({"model": "stale", "labels": {"liver": 1}}))
    _training_env(monkeypatch, host, {"XNW_RESOURCE_MODEL": "*.pt,*.json"})
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    creates = [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    model_xml = creates[1]["body"].decode()
    assert "<analysis:model_name>segmentation_spleen</analysis:model_name>" in model_xml and "stale" not in model_xml
    uploads = sorted(c["path"].split("?")[0] for c in handler.calls if c["method"] == "PUT" and "/resources/MODEL" in c["path"])
    assert "/data/experiments/XNAT_E88888/resources/MODEL_CARD/files/model-card.json" in uploads
    assert "/data/experiments/XNAT_E88888/resources/MODEL/files/backup-model-card.json" in uploads, "a view file, so it rides with the weights"
    assert "/data/experiments/XNAT_E88888/resources/MODEL/files/model-card.json" not in uploads


def test_a_card_field_the_xml_cannot_encode_is_a_registration_error_not_an_abort(cs, tmp_path, monkeypatch):
    """A lone surrogate in a card field builds fine and fails at xml.encode(); the best-effort promise
    means an error in the outcome and a written manifest, not an aborted wrapup (Codex P2, PR #21 round 15)."""
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    (inp / "model-card.json").write_text('{"model_framework": "monailabel", "model": "\\ud800", "labels": {"spleen": 1}}')
    _training_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["analysis_record"]["id"] == "XNAT_E77777"
    assert "surrogates not allowed" in manifest["trained_model"]["error"] or "codec can't encode" in manifest["trained_model"]["error"]
    creates = [c["path"] for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    assert len(creates) == 1, "the run record only; no model asset"


def test_a_zero_byte_model_card_is_not_advertised(cs, tmp_path, monkeypatch):
    """publish_record skips zero-byte files; the record must not name a MODEL_CARD resource that holds
    nothing, nor the manifest say model_card: true (Codex P2, PR #21 round 12)."""
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    (inp / "model-card.json").write_bytes(b"")
    _training_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    creates = [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    model_xml = creates[1]["body"].decode()
    assert "model_card_resource_label" not in model_xml
    uploads = [c["path"].split("?")[0] for c in handler.calls if c["method"] == "PUT" and "/resources/MODEL_CARD/" in c["path"]]
    assert uploads == []
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["trained_model"]["model_card"] is False and manifest["trained_model"]["id"] == "XNAT_E88888"


def test_a_zero_byte_checkpoint_is_not_a_weight(cs, tmp_path, monkeypatch):
    """publish_record skips zero-byte files; a model whose `weights` or default_checkpoint named one
    would point consumers at a file the MODEL resource does not hold (Codex P2, PR #21 round 26).
    Empty checkpoints are left off; with none left, no model is registered and the run still is."""
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    (inp / "interrupted.pt").write_bytes(b"")
    _training_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    creates = [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    model_xml = creates[1]["body"].decode()
    assert "<analysis:default_checkpoint>segmentation_spleen.pt</analysis:default_checkpoint>" in model_xml
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["trained_model"]["weights"] == ["segmentation_spleen.pt"], "the empty file is not a weight"
    uploads = sorted(c["path"].split("?")[0].rsplit("/", 1)[-1] for c in handler.calls if c["method"] == "PUT" and "/resources/MODEL/" in c["path"])
    assert "interrupted.pt" not in uploads and "segmentation_spleen.pt" in uploads
    # the declared default itself empty and another weight present: the empty file is not a candidate;
    # the bare-basename rule then hands the only usable weight over, as for any stale basename
    handler.calls.clear(); out2 = tmp_path / "out2"
    (inp / "segmentation_spleen.pt").write_bytes(b""); (inp / "interrupted.pt").write_bytes(b"OTHER")
    assert proc.main(["--input", str(inp), "--output", str(out2)]) == 0
    creates = [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    assert "<analysis:default_checkpoint>interrupted.pt</analysis:default_checkpoint>" in creates[1]["body"].decode(), "never the empty file"
    assert json.loads((out2 / "wrapup.json").read_text())["trained_model"]["weights"] == ["interrupted.pt"]
    # every checkpoint empty: the run record is published, no model asset, the manifest says why
    handler.calls.clear(); out3 = tmp_path / "out3"
    (inp / "interrupted.pt").write_bytes(b"")
    assert proc.main(["--input", str(inp), "--output", str(out3)]) == 0
    creates = [c for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    assert len(creates) == 1, "the run record only; no model asset"
    error = json.loads((out3 / "wrapup.json").read_text())["trained_model"]["error"]
    assert "MODEL" in error and ("no usable checkpoint" in error or "no MODEL view files" in error), error
    from segwrapup.model import register_trained_model
    from segwrapup.register import XnatContext
    context = XnatContext(host=host, user="u", password="p", project="PROJ_1", session="", dataset="XNAT_D0001")
    outcome = register_trained_model(context, inp, None, {"id": "XNAT_E77777", "id_is_accession": True}, "SUCCEEDED",
                                     {"MODEL": ["segmentation_spleen.pt", "interrupted.pt"]}, {}, {}, 5.0)
    assert "no usable checkpoint" in outcome["error"] and "interrupted.pt" in outcome["error"], outcome


def test_run_data_fields_of_the_wrong_shape_are_a_link_error_not_an_abort(cs, tmp_path, monkeypatch):
    """A null or list data_fields on the run record must surface as link_error in the outcome
    (RuntimeError, which register_trained_model handles), not an AttributeError after run and model
    exist (Codex P2, PR #21 round 11)."""
    import urllib.request
    from segwrapup.model import _record_fields
    from segwrapup.register import XnatContext
    host, handler = cs
    context = XnatContext(host=host, user="u", password="p", project="PROJ_1", session="", dataset="XNAT_D0001")
    real = urllib.request.urlopen
    class Answer:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    for shape in (None, ["a", "b"], "text"):
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None, s=shape: Answer(json.dumps({"items": [{"data_fields": s}]}).encode()))
        with pytest.raises(RuntimeError, match="data_fields is .*, not an object"):
            _record_fields(context, "XNAT_E77777", 5.0)
    # valid JSON nested past the recursion limit is a RuntimeError too, not a RecursionError past the guards (round 28)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Answer(b'{"items": [' + b"[" * 100000 + b"]" * 100000 + b"]}"))
    with pytest.raises(RuntimeError, match="failed: maximum recursion depth|failed: RecursionError"):
        _record_fields(context, "XNAT_E77777", 5.0)
    # the provenance scratch that cannot be made is an outcome, not an abort after the run record (round 28)
    from segwrapup import model as model_module
    from segwrapup.model import register_trained_model
    inp = _training_output(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", real)
    def no_space(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(model_module.tempfile, "mkdtemp", no_space)
    outcome = register_trained_model(context, inp, None, {"id": "XNAT_E77777", "id_is_accession": True}, "SUCCEEDED",
                                     {"MODEL": ["segmentation_spleen.pt", "model-card.json"]}, {}, {}, 5.0)
    assert "No space left on device" in outcome["error"] and outcome["label"], outcome
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Answer(json.dumps({"items": [{"data_fields": {"results_json": "{}"}}]}).encode()))
    assert _record_fields(context, "XNAT_E77777", 5.0) == {"results_json": "{}"}
    monkeypatch.setattr(urllib.request, "urlopen", real)


def test_the_link_step_accepts_json_native_results_fields():
    """XNAT answers results_json as text, but a dict or a number in its place must not raise TypeError
    out of the link step after run and model exist (Codex P2, PR #21 round 14)."""
    from segwrapup.model import _current_results
    assert _current_results("XNAT_E1", None) == {} and _current_results("XNAT_E1", "") == {}
    assert _current_results("XNAT_E1", '{"views": {"MODEL": ["a.pt"]}}') == {"views": {"MODEL": ["a.pt"]}}
    deep = "[" * 100000 + "]" * 100000
    assert _current_results("XNAT_E1", deep) == {"results_raw": deep}, "nested past the recursion limit: kept raw, not an abort (round 29)"
    assert _current_results("XNAT_E1", {"views": {}}) == {"views": {}}, "a dict is taken as is"
    assert _current_results("XNAT_E1", 7) == {"results": 7} and _current_results("XNAT_E1", [1, 2]) == {"results": [1, 2]}
    assert _current_results("XNAT_E1", "[1, 2]") == {"results": [1, 2]}
    assert _current_results("XNAT_E1", "{not json") == {"results_raw": "{not json"}


def test_the_run_link_is_keyed_on_the_accession_resolved_from_the_lookup(monkeypatch):
    """publish_record hands back the run's label as its id when XNAT answered no id; the partial XML
    update is keyed on the accession, so the lookup goes by label under the project and the answer's
    ID is what the document and the PUT use (Codex P2, PR #21 round 16)."""
    import urllib.request
    from segwrapup import model
    from segwrapup.register import XnatContext
    context = XnatContext(host="http://x", user="u", password="p", project="PROJ_1", session="", dataset="XNAT_D0001")
    seen = {}
    class Answer:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def urlopen(req, timeout=None):
        seen["get"] = req.full_url
        return Answer(json.dumps({"items": [{"data_fields": {"ID": "XNAT_E77777", "label": "monailabel-train_ds_20260926T221538Z_record", "results_json": "{}"}}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(model, "_put", lambda ctx, url, body, ctype, timeout: (seen.update(put=url, xml=body.decode()), (200, "XNAT_E77777"))[1])
    accession = model.note_model_on_run(context, "monailabel-train_ds_20260926T221538Z_record", "XNAT_E88888", "model_x")
    assert accession == "XNAT_E77777"
    assert seen["get"] == "http://x/data/projects/PROJ_1/experiments/monailabel-train_ds_20260926T221538Z_record?format=json", "a label is looked up under the project"
    assert seen["put"].startswith("http://x/data/experiments/XNAT_E77777?") and 'ID="XNAT_E77777"' in seen["xml"]
    # an accession id is looked up directly, as before
    assert model.note_model_on_run(context, "XNAT_E77777", "XNAT_E88888", "model_x") == "XNAT_E77777"
    assert seen["get"] == "http://x/data/experiments/XNAT_E77777?format=json"
    # no resolvable accession in the answer: a link error, never a document keyed on the label
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Answer(json.dumps({"items": [{"data_fields": {"label": "l", "results_json": "{}"}}]}).encode()))
    seen.pop("put")
    with pytest.raises(RuntimeError, match="accession id could not be resolved"):
        model.note_model_on_run(context, "monailabel-train_ds_20260926T221538Z_record", "XNAT_E88888", "model_x")
    assert "put" not in seen


def test_the_link_document_stays_under_the_cap_when_the_runs_results_cannot_be_parsed(monkeypatch):
    """The link step re-reads the run'"'"'s ``results_json`` and keeps what it cannot parse as raw text.
    That value is not reduced by the view counts, so the merged document stayed over the 65,536-character
    schema cap, XNAT refused the PUT, and the run was left without its forward link to the model that had
    just been registered (Codex P2, PR #21 round 30)."""
    import urllib.request
    from segwrapup import model
    from segwrapup.publish import RESULTS_JSON_MAX
    from segwrapup.register import XnatContext
    context = XnatContext(host="http://x", user="u", password="p", project="PROJ_1", session="", dataset="XNAT_D0001")
    seen = {}
    class Answer:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    unparseable = "[" * 100000 + "]" * 100000   # valid JSON, nested past the parser'"'"'s limit: kept raw
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Answer(
        json.dumps({"items": [{"data_fields": {"ID": "XNAT_E77777", "label": "run_x", "results_json": unparseable}}]}).encode()))
    monkeypatch.setattr(model, "_put", lambda ctx, url, body, ctype, timeout: (seen.update(xml=body.decode()), (200, "XNAT_E77777"))[1])
    assert model.note_model_on_run(context, "XNAT_E77777", "XNAT_E88888", "model_x") == "XNAT_E77777"
    written = seen["xml"].split("<analysis:results_json>")[1].split("</analysis:results_json>")[0]
    results = json.loads(written.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    assert len(json.dumps(results)) <= RESULTS_JSON_MAX, "the document XNAT is asked to store is within the cap"
    assert results["trained_model"]["id"] == "XNAT_E88888", "and it carries the forward link, which is the point of the PUT"
    assert results["truncated"] == ["results_raw"] and "results_raw" not in results


def test_fallback_labels_are_resolved_to_accessions_before_the_model_names_the_run_or_the_run_the_model(tmp_path, monkeypatch):
    """publish_record hands back a label when XNAT's create answers no id. The model's reverse link
    (engine_metadata_json.source_run_id, provenance.json) and the run's forward link
    (results_json.trained_model.id) must both be accessions a consumer can GET (Codex P2, PR #21 round 17)."""
    from segwrapup import model
    from segwrapup.register import XnatContext
    context = XnatContext(host="http://x", user="u", password="p", project="PROJ_1", session="", dataset="XNAT_D0001")
    out = tmp_path / "out"; (out / "raw").mkdir(parents=True)
    (out / "raw" / "segmentation_spleen.pt").write_bytes(b"W")
    lookups, published, links = [], {}, {}
    def record_fields(ctx, record_id, timeout, by_label=None):
        lookups.append(record_id)
        run = {"ID": "XNAT_E77777", "label": "run_lbl_x", "results_json": "{}"}
        return {"run_lbl_x": run, "XNAT_E77777": run, "model_ds_x": {"ID": "XNAT_E88888", "label": "model_ds_x"}}[record_id]
    monkeypatch.setattr(model, "_record_fields", record_fields)
    monkeypatch.setattr(model, "_username", lambda ctx, timeout: "jdickson")
    monkeypatch.setattr(model, "model_label", lambda dataset_label, when=None, run_id=None: "model_ds_x")
    def publish_record(ctx, label, xml, files, timeout_seconds=300.0, xsi_type=None):
        published["xml"] = xml
        published["provenance"] = json.loads(files["PROVENANCE"][0].path.read_text())
        return {"id": label, "label": label}   # XNAT answered no id: the label stands in
    monkeypatch.setattr(model, "publish_record", publish_record)
    monkeypatch.setattr(model, "_put", lambda ctx, url, body, ctype, timeout: (links.update(url=url, xml=body.decode()), (200, "XNAT_E77777"))[1])
    result = model.register_trained_model(context, out, "raw", {"id": "run_lbl_x", "label": "run_lbl_x"}, "SUCCEEDED",
                                          {"MODEL": ["segmentation_spleen.pt"]}, {"pipeline": "monailabel-train"}, {"label": "ds"})
    assert lookups[0] == "run_lbl_x", "the run is resolved before the model is built"
    assert '"source_run_id": "XNAT_E77777"' in published["xml"].replace("&quot;", '"') and published["provenance"]["source_run_id"] == "XNAT_E77777"
    assert "run_lbl_x" not in published["xml"].split("<analysis:engine_metadata_json>")[1].split("</analysis:engine_metadata_json>")[0]
    assert result["id"] == "XNAT_E88888" and result["source_run_id"] == "XNAT_E77777" and result["linked"] is True
    assert '"id": "XNAT_E88888"' in links["xml"].replace("&quot;", '"') and links["url"].startswith("http://x/data/experiments/XNAT_E77777?")
    # a label that looks like an accession is still looked up by label when the create answered no id (round 19)
    lookups.clear()
    record_fields_by = {}
    def record_fields_flagged(ctx, record_id, timeout, by_label=None):
        record_fields_by[record_id] = by_label
        lookups.append(record_id)
        return {"ID": "XNAT_E77777", "label": record_id, "results_json": "{}"} if by_label else {"ID": record_id, "label": "l", "results_json": "{}"}
    monkeypatch.setattr(model, "_record_fields", record_fields_flagged)
    result = model.register_trained_model(context, out, "raw", {"id": "MY_SITE_E123", "label": "MY_SITE_E123", "id_is_accession": False}, "SUCCEEDED",
                                          {"MODEL": ["segmentation_spleen.pt"]}, {"pipeline": "monailabel-train"}, {"label": "ds"})
    assert record_fields_by["MY_SITE_E123"] is True and result["source_run_id"] == "XNAT_E77777"
    assert '"source_run_id": "XNAT_E77777"' in published["xml"].replace("&quot;", '"')
    result = model.register_trained_model(context, out, "raw", {"id": "MY_SITE_E123", "label": "l", "id_is_accession": True}, "SUCCEEDED",
                                          {"MODEL": ["segmentation_spleen.pt"]}, {"pipeline": "monailabel-train"}, {"label": "ds"})
    assert result["source_run_id"] == "MY_SITE_E123", "trusted as the create's own answer"
    assert record_fields_by.get("MY_SITE_E123") is not True, "never looked up by label"
    # a run label that resolves to nothing: no model is registered at all
    monkeypatch.setattr(model, "_record_fields", lambda ctx, record_id, timeout, by_label=None: {"label": record_id})
    result = model.register_trained_model(context, out, "raw", {"id": "run_lbl_y", "label": "run_lbl_y"}, "SUCCEEDED",
                                          {"MODEL": ["segmentation_spleen.pt"]}, {"pipeline": "monailabel-train"}, {"label": "ds"})
    assert "accession id could not be resolved" in result["error"]


def test_a_card_nested_past_the_recursion_limit_is_a_registration_outcome_not_an_abort(cs, tmp_path, monkeypatch):
    """build_model_xml runs inside the guarded block now: whatever the card does to the document
    builder ends as trained_model.error with the manifest written (Codex P2, PR #21 round 20)."""
    from segwrapup import model
    host, handler = cs
    inp, out = _training_output(tmp_path), tmp_path / "out"
    _training_env(monkeypatch, host)
    def exploding(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(model, "build_model_xml", exploding)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["analysis_record"]["id"] == "XNAT_E77777" and "recursion depth" in manifest["trained_model"]["error"]
    creates = [c["path"] for c in handler.calls if c["method"] == "PUT" and c["path"].startswith("/data/projects/PROJ_1/experiments/") and "/resources/" not in c["path"]]
    assert len(creates) == 1
