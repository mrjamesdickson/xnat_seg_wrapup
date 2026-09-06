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
CONTAINERS = [
    {"id": 900, "workflow-id": "5000", "subtype": "docker", "status": "Complete", "docker-image": "radiomics/pyradiomics:CLI",
     "history": [{"status": "Created", "time-recorded": "2026-09-06T10:00:00.000+0000"},
                 {"status": "Complete", "time-recorded": "2026-09-06T10:02:30.000+0000"}]},
    {"id": 901, "workflow-id": "5001", "subtype": "docker-wrapup", "status": "Running", "docker-image": "xnatworks/proc-wrapup:0.4.0"},
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

    def do_GET(self):
        self._record()
        if self.path == "/xapi/containers":
            self._send(200, json.dumps(CONTAINERS).encode(), "application/json")
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
    inp = tool_output(tmp_path, with_status={"exit_code": 0, "workflow_id": "5000"})
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
    assert manifest["execution"]["container_id"] == 900 and manifest["execution"]["duration_seconds"] == 150
    # a report a reviewer can read, interpreting nothing
    report = (out / "report.html").read_text()
    assert "PyRadiomics 3.1" in report and "SUCCEEDED" in report and "raw/features.csv" in report and "line one" in report
    # the record: generic fields, roles from the card + defaults, DERIVED takes the rest, one session
    creates = [c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"]]
    assert len(creates) == 1 and creates[0]["path"].startswith("/data/experiments/XNAT_E00018/assessors/PyRadiomics_scan3_")
    xml = creates[0]["body"].decode()
    for fragment in ("<analysis:pipeline_name>PyRadiomics<", "<analysis:pipeline_version>3.1<", "<analysis:analysis_type>radiomics<",
                     "<analysis:run_status>SUCCEEDED<", "<analysis:auto_qc_status>NOT_EVALUATED<", "<analysis:container_id>900<",
                     "<analysis:duration_seconds>150<", "<analysis:card_id>pyradiomics<", "<analysis:scans><analysis:scan>3<"):
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
    inp = tool_output(tmp_path, with_status={"exit_code": 2, "workflow_id": "5000"})
    out = tmp_path / "out"
    set_env(monkeypatch, host)
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    xml = [c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"]][0]["body"].decode()
    assert "<analysis:run_status>FAILED<" in xml and "<analysis:auto_qc_status>FAIL<" in xml
    assert "FAILED" in (out / "report.html").read_text()


def test_proc_wrapup_without_status_json_finds_the_parent_by_workflow_order(cs, tmp_path, monkeypatch):
    host, handler = cs
    inp = tool_output(tmp_path)
    out = tmp_path / "out"
    set_env(monkeypatch, host)      # XNAT_WORKFLOW_ID=5001 -> parent 5000
    assert proc.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["run_status"] == "SUCCEEDED" and manifest["execution"]["container_id"] == 900
    assert (out / "logs" / "stderr.log").exists()


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
