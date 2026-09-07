"""proc-wrapup: keep everything the tool wrote, capture how the run went, report, publish.

A fake XNAT + Container Service on localhost records every request (same style as
test_publish.py) and answers the container list and log endpoints.
"""
import json
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
                "XNW_RESOURCE_METRICS": "raw/features.csv"}
CONTEXT_ENV = {"XNAT_HOST": "http://x", "XNAT_USER": "alias", "XNAT_PASS": "secret",
               "PROC_PROJECT": "PROJ_1", "PROC_SESSION_ID": "XNAT_E00018", "PROC_SCAN_ID": "3", "XNAT_WORKFLOW_ID": "5001"}
# The parent (900) ran under workflow 4990; a stranger's run (902) took workflow 5000, the id
# just before the wrapup's own 5001, so any "previous workflow id" guess picks the wrong run.
# The real link is the mount: CS resolves the wrapup's /input from the parent's output mount
# (same xnat-host-path on both), as seen on demo02 for containers 35531/35532.
BUILD = "/data/xnat/build/1727a620-4b73-4aad-872d-681a14a98d77"
SETUP_OUT = "/data/xnat/build/0c0c-setup-output"
CONTAINERS = [
    {"id": 900, "workflow-id": "4990", "subtype": "docker", "status": "Complete", "docker-image": "radiomics/pyradiomics:CLI",
     "backend": "swarm", "node-id": "laz2ephdgvajpbg96rhc6lfmn", "service-id": "svc1", "task-id": "task1", "container-id": "5687d46dcb7c", "user-id": "admin",
     "reserve-memory": 256, "limit-memory": 1024, "limit-cpu": 1.0, "generic-resources": {"GPU": "1"},
     "mounts": [{"name": "input-mount", "writable": False, "xnat-host-path": SETUP_OUT},
                {"name": "output-mount", "writable": True, "xnat-host-path": BUILD}],
     "history": [{"status": "Created", "time-recorded": "2026-09-06T10:00:00.000+0000"},
                 {"status": "running", "time-recorded": "2026-09-06T10:00:20.000+0000"},
                 {"status": "complete", "time-recorded": "2026-09-06T10:02:20.000+0000"},
                 {"status": "Complete", "time-recorded": "2026-09-06T10:02:30.000+0000"}]},
    {"id": 899, "workflow-id": "4991", "subtype": "docker-setup", "status": "Complete", "docker-image": "xnatworks/record-fetch:0.5.0",
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
        elif self.path == "/data/experiments/XNAT_E00018?format=json":
            self._send(200, json.dumps({"items": [{"data_fields": {"label": "SESS01"}}]}).encode(), "application/json")
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
        self._send(201 if "/assessors/" in self.path and "/out/" not in self.path else 200,
                   b"XNAT_E77777" if "/out/" not in self.path else b"")

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

    # everything the tool wrote, verbatim, under raw/; the DICOM copy not
    assert (out / "raw" / "features.csv").exists() and (out / "raw" / "sub" / "log.txt").exists()
    assert not (out / "raw" / ".source_dicom").exists()
    # execution state: status carried, parent logs fetched from CS by the workflow id in status.json
    assert json.loads((out / "status.json").read_text())["exit_code"] == 0
    assert (out / "logs" / "stdout.log").read_text().startswith("line one") and (out / "logs" / "stderr.log").exists()
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["wrapup"] == "proc-wrapup" and manifest["run_status"] == "SUCCEEDED"
    assert manifest["execution"]["container_id"] == 900 and manifest["execution"]["duration_seconds"] == 120
    # a report a reviewer can read, interpreting nothing
    report = (out / "report.html").read_text()
    assert "PyRadiomics 3.1" in report and "SUCCEEDED" in report and "raw/features.csv" in report and "line one" in report
    # the record: generic fields, roles from the card + defaults, DERIVED takes the rest, one session
    creates = [c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"]]
    # the label carries the session label: labels are unique per project, not per session
    assert len(creates) == 1 and creates[0]["path"].startswith("/data/experiments/XNAT_E00018/assessors/PyRadiomics_SESS01_scan3_")
    xml = creates[0]["body"].decode()
    for fragment in ("<analysis:pipeline_name>PyRadiomics<", "<analysis:pipeline_version>3.1<", "<analysis:analysis_type>radiomics<",
                     "<analysis:run_status>SUCCEEDED<", "<analysis:auto_qc_status>NOT_EVALUATED<", "<analysis:container_id>900<",
                     f"<analysis:wrapup_version>proc-wrapup {__version__}<",
                     "<analysis:duration_seconds>120<", "<analysis:card_id>pyradiomics<", "<analysis:scans><analysis:scan>3<"):
        assert fragment in xml, fragment
    uploads = [c["path"].split("/out/resources/")[1].split("?")[0] for c in handler.calls if "/out/resources/" in c["path"]]
    assert "METRICS/files/raw/features.csv" in uploads and "REPORT/files/report.html" in uploads
    assert "PROVENANCE/files/wrapup.json" in uploads and "PROVENANCE/files/status.json" in uploads
    assert "LOGS/files/logs/stdout.log" in uploads and "DERIVED/files/raw/sub/log.txt" in uploads
    assert manifest["analysis_record"]["id"] == "XNAT_E77777"
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
    assert any("METRICS/files/raw/features.csv" in u for u in uploads) and any("DERIVED/files/raw/sub/log.txt" in u for u in uploads)
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
    assert [ (p["container_id"], p["seconds"]) for p in config["phases"]["setup"] ] == [(899, 6)]      # found by the shared mount
    assert config["phases"]["wrapup"]["container_id"] == 901 and config["phases"]["wrapup"]["seconds"] is not None   # still running: to now
    assert config["total_seconds"] == 120 + 6 + config["phases"]["wrapup"]["seconds"]
    assert "measured usage" in config["billing_note"]
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["execution"]["facts"]["node_id"] == "laz2ephdgvajpbg96rhc6lfmn"
    report = (out / "report.html").read_text()
    assert "laz2ephdgvajpbg96rhc6lfmn" in report and "swarm" in report


def test_pointer_only_keeps_files_an_explicit_derived_contract_left_off_the_record(cs, tmp_path, monkeypatch, caplog):
    """Codex P1 on PR #10: with XNW_RESOURCE_DERIVED naming only some outputs, the rest must not vanish."""
    host, handler = cs
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "4990"})
    out = tmp_path / "out"
    set_env(monkeypatch, host, {"PROC_PIPELINE_NAME": "pyradiomics", "XNW_RESOURCE_DERIVED": "raw/features.csv"})
    with caplog.at_level(logging.WARNING):
        assert proc.main(["--input", str(inp), "--output", str(out), "--pointer-only"]) == 0
    uploads = [c["path"] for c in handler.calls if "/out/resources/" in c["path"]]
    assert not any("sub/log.txt" in u for u in uploads)                      # off the record by contract
    assert (out / "raw" / "sub" / "log.txt").exists()                          # so it stays in the output
    assert not (out / "raw" / "features.csv").exists()                        # what the record holds is removed
    assert "2 file(s) are not on record XNAT_E77777 (outside the contract) and stay in the output: raw/status.json, raw/sub/log.txt" in caplog.text
