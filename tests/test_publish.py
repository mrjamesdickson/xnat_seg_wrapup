"""The analysis record (analysis:sessionAnalysisData) the wrapup publishes when a card opts in.

Uses the same recording HTTP server as test_register.py: every PUT is captured with its
path, auth and body, so the tests check the exact REST calls a real XNAT would receive.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest

from segwrapup import __version__, cli
from segwrapup.publish import (
    XSI_TYPE, RecordContract, build_record_xml, collect_files, publish_record,
)
from segwrapup.register import XnatContext
from tests.conftest import blob_mask, series_ras_affine, write_ct_series, write_mask

CONTRACT = {
    "card_id": "deepwmh", "card_revision": "1.0.0", "contract_version": "0.1",
    "analysis_type": "segmentation", "container_image": "vnmd/deepwmh_1.0.1:20260826",
    "container_digest": "sha256:" + "a" * 64, "output_resource_label": "DEEPWMH",
}
CONTEXT_ENV = {"XNAT_HOST": "http://x", "XNAT_USER": "alias", "XNAT_PASS": "secret",
               "SEG_PROJECT": "PROJ_1", "SEG_SESSION_ID": "XNAT_E00018", "SEG_SCAN_ID": "2"}


class _Handler(BaseHTTPRequestHandler):
    calls: list = []
    fail_paths: set = set()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _Handler.calls.append({"path": self.path, "auth": self.headers.get("Authorization"),
                               "content_type": self.headers.get("Content-Type"), "body": body})
        if any(self.path.startswith(p) for p in _Handler.fail_paths):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        self.send_response(201 if "/assessors/" in self.path and "/out/" not in self.path else 200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        # XNAT answers an assessor create with the new accession ID as plain text.
        self.wfile.write(b"XNAT_E99999" if "/out/" not in self.path else b"")

    def log_message(self, *args):
        pass


@pytest.fixture
def xnat():
    _Handler.calls, _Handler.fail_paths = [], set()
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", _Handler
    server.shutdown()


def _context(host):
    return XnatContext(host=host, user="alias", password="secret", project="PROJ_1", session="XNAT_E00018", scan="2")


def _report():
    return {"model": "DeepWMH", "model_version": "1.0.1", "session": "S", "scan": "2", "results": []}


def _results():
    return [{"file": "segmentation.nii.gz", "structures": [{"label": 1, "name": "WMH", "voxels": 10, "volume_ml": 15.19}],
             "total_volume_ml": 15.19}]


# ── contract ───────────────────────────────────────────────────────────────────

def test_contract_absent_means_no_record(caplog):
    with caplog.at_level(logging.INFO):
        assert RecordContract.from_env({}) is None
    assert "no XNW_CONTRACT" in caplog.text


def test_contract_parses_fields_and_resource_overrides():
    env = {"XNW_CONTRACT": json.dumps({**CONTRACT, "resources": {"metrics": ["*.json"], "LOGS": ["*.log"]}})}
    contract = RecordContract.from_env(env)
    assert contract.card_id == "deepwmh" and contract.container_digest.startswith("sha256:")
    assert contract.resources["METRICS"] == ["*.json"]          # override, upper-cased role
    assert contract.resources["LOGS"] == ["*.log"]              # new role
    assert contract.resources["REPORT"] == ["report.html"]      # default kept


@pytest.mark.parametrize("raw", ["not json", "[1,2]", json.dumps({"resources": {"METRICS": "volumes.json"}})])
def test_contract_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        RecordContract.from_env({"XNW_CONTRACT": raw})


# ── files and XML ──────────────────────────────────────────────────────────────

def test_collect_files_follows_contract_roles_and_skips_missing(tmp_path):
    for name in ("volumes.json", "report.html", "wrapup.json", "segmentation.nii.gz"):
        (tmp_path / name).write_text("x")
    files = collect_files(tmp_path, RecordContract())
    assert {role: [p.name for p in paths] for role, paths in files.items()} == {
        "METRICS": ["volumes.json"], "REPORT": ["report.html"], "PROVENANCE": ["wrapup.json"]}
    # the mask is NOT carried: it stays on the parent's resource


def test_record_xml_holds_type_status_qc_provenance_and_no_measurement_fields(tmp_path):
    (tmp_path / "volumes.json").write_text("{}")
    contract = RecordContract.from_env({"XNW_CONTRACT": json.dumps(CONTRACT)})
    xml = build_record_xml(_context("http://x"), contract, "DeepWMH_scan2_X", _report(), _results(),
                           collect_files(tmp_path, contract), source_dicom_present=True)
    assert 'project="PROJ_1" label="DeepWMH_scan2_X"' in xml
    assert "<xnat:imageSession_ID>XNAT_E00018</xnat:imageSession_ID>" in xml
    for fragment in ("<analysis:pipeline_name>DeepWMH<", "<analysis:pipeline_version>1.0.1<",
                     "<analysis:container_image>vnmd/deepwmh_1.0.1:20260826<", "<analysis:card_id>deepwmh<",
                     "<analysis:run_status>SUCCEEDED<", "<analysis:publication_status>DRAFT<",
                     "<analysis:review_state>PENDING_REVIEW<", "<analysis:auto_qc_status>PASS<",
                     "<analysis:output_resource_label>DEEPWMH<", "<analysis:output_file_count>1<",
                     "<analysis:scans><analysis:scan>2</analysis:scan></analysis:scans>",
                     f"<analysis:wrapup_version>seg-wrapup {__version__}<"):
        assert fragment in xml, fragment
    # no algorithm-specific element, ever: the numbers are in results_json/METRICS only
    assert "<analysis:volume" not in xml and "<analysis:structures" not in xml
    summary = json.loads(xml.split("<analysis:results_json>")[1].split("</analysis:results_json>")[0]
                         .replace("&quot;", '"'))
    assert summary["total_volume_ml"] == 15.19 and summary["structures"] == 1


def test_record_xml_escapes_and_warns_when_nothing_was_measured():
    contract = RecordContract(card_id="a&b<c>")
    xml = build_record_xml(_context("http://x"), contract, "L", _report(), [], {}, source_dicom_present=False)
    assert "<analysis:card_id>a&amp;b&lt;c&gt;</analysis:card_id>" in xml
    assert "<analysis:auto_qc_status>WARN<" in xml


# ── REST calls ─────────────────────────────────────────────────────────────────

def test_publish_creates_record_then_uploads_files_to_its_out_resources(xnat, tmp_path):
    host, handler = xnat
    (tmp_path / "volumes.json").write_text('{"a":1}')
    (tmp_path / "report.html").write_text("<html/>")
    files = collect_files(tmp_path, RecordContract())
    outcome = publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", files)
    assert outcome["id"] == "XNAT_E99999" and outcome["xsi_type"] == XSI_TYPE
    paths = [c["path"] for c in handler.calls]
    assert paths[0] == "/data/experiments/XNAT_E00018/assessors/DeepWMH_scan2_X?inbody=true"
    assert handler.calls[0]["content_type"] == "application/xml" and handler.calls[0]["body"] == b"<xml/>"
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/METRICS/files/volumes.json?inbody=true&format=JSON" in paths
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/REPORT/files/report.html?inbody=true&format=HTML" in paths
    assert all(c["auth"].startswith("Basic ") for c in handler.calls)
    assert outcome["uploaded"] == {"METRICS": ["volumes.json"], "REPORT": ["report.html"]}


def test_publish_raises_with_http_detail(xnat, tmp_path):
    host, handler = xnat
    handler.fail_paths = {"/data/experiments/XNAT_E00018/assessors/"}
    with pytest.raises(RuntimeError, match="HTTP 500 boom"):
        publish_record(_context(host), "L", "<xml/>", {})


# ── CLI end to end ─────────────────────────────────────────────────────────────

def _run_with_masks(tmp_path, monkeypatch, env, *extra):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    write_ct_series(inp / ".source_dicom")
    write_mask(inp / "segmentation.nii.gz", blob_mask(), affine=series_ras_affine())
    for key in list(CONTEXT_ENV) + ["XNW_CONTRACT", "SEG_NO_REGISTER", "SEG_NO_PUBLISH"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = cli.main(["--input", str(inp), "--output", str(out), "--model", "DeepWMH", "--model-version", "1.0.1",
                     "--no-dicom-seg", *extra])
    return code, json.loads((out / "wrapup.json").read_text()), out


def test_cli_publishes_record_when_contract_and_context_present(xnat, tmp_path, monkeypatch):
    host, handler = xnat
    env = {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CONTRACT": json.dumps(CONTRACT)}
    code, manifest, out = _run_with_masks(tmp_path, monkeypatch, env)
    assert code == 0
    record = manifest["analysis_record"]
    assert record["id"] == "XNAT_E99999" and record["label"].startswith("DeepWMH_scan2_")
    assert "volumes.json" in record["uploaded"]["METRICS"] and "report.html" in record["uploaded"]["REPORT"]
    assert "wrapup.json" in record["uploaded"]["PROVENANCE"]
    create = handler.calls[0]
    assert create["path"].endswith(f"/assessors/{record['label']}?inbody=true")
    assert b"<analysis:pipeline_name>DeepWMH</analysis:pipeline_name>" in create["body"]
    assert b"<analysis:card_id>deepwmh</analysis:card_id>" in create["body"]


def test_cli_without_contract_publishes_nothing(xnat, tmp_path, monkeypatch, caplog):
    host, handler = xnat
    with caplog.at_level(logging.INFO):
        code, manifest, _ = _run_with_masks(tmp_path, monkeypatch, {**CONTEXT_ENV, "XNAT_HOST": host})
    assert code == 0 and manifest["analysis_record"] is None
    assert not any("/assessors/" in c["path"] for c in handler.calls)
    assert "no XNW_CONTRACT" in caplog.text


def test_cli_publish_failure_is_recorded_not_fatal(xnat, tmp_path, monkeypatch, caplog):
    host, handler = xnat
    handler.fail_paths = {"/data/experiments/XNAT_E00018/assessors/"}
    env = {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CONTRACT": json.dumps(CONTRACT)}
    with caplog.at_level(logging.ERROR):
        code, manifest, out = _run_with_masks(tmp_path, monkeypatch, env)
    assert code == 0                                    # masks and report still delivered
    assert (out / "volumes.json").exists()
    assert "error" in manifest["analysis_record"] and "HTTP 500" in manifest["analysis_record"]["error"]
    assert "not published" in caplog.text


def test_cli_no_publish_flag(xnat, tmp_path, monkeypatch):
    host, handler = xnat
    env = {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CONTRACT": json.dumps(CONTRACT)}
    code, manifest, _ = _run_with_masks(tmp_path, monkeypatch, env, "--no-publish")
    assert code == 0 and manifest["analysis_record"] is None and handler.calls == []
