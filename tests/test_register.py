import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from segwrapup import cli, register
from tests.conftest import blob_mask, series_ras_affine, write_ct_series, write_mask


class _RecordingHandler(BaseHTTPRequestHandler):
    calls: list = []
    status_code = 200

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _RecordingHandler.calls.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "length": len(body), "first_bytes": body[128:132]}
        )
        self.send_response(_RecordingHandler.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"label": "ok"}).encode())

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def xnat_server():
    _RecordingHandler.calls = []
    _RecordingHandler.status_code = 200
    server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _RecordingHandler
    server.shutdown()


def test_context_requires_all_variables(caplog):
    with caplog.at_level(logging.INFO):
        assert register.XnatContext.from_env({"XNAT_HOST": "h", "XNAT_USER": "u"}) is None
    assert "XNAT_PASS" in caplog.text and "SEG_PROJECT" in caplog.text
    context = register.XnatContext.from_env(
        {"XNAT_HOST": "http://x/", "XNAT_USER": "u", "XNAT_PASS": "p", "SEG_PROJECT": "P1", "SEG_SESSION_ID": "XNAT_E1", "SEG_SCAN_ID": "2"}
    )
    assert context == register.XnatContext("http://x", "u", "p", "P1", "XNAT_E1", "2")


def test_collection_label_is_safe_and_readable():
    when = datetime(2026, 9, 2, 18, 45, tzinfo=timezone.utc)
    assert register.collection_label("TotalSegmentator", "2", when) == "TotalSegmentator_scan2_20260902T184500Z"
    assert register.collection_label("spleen ct/seg", "", when) == "spleen_ct_seg_20260902T184500Z"
    assert len(register.collection_label("x" * 100, "1", when)) <= 64


def test_register_puts_file_with_basic_auth(xnat_server, tmp_path):
    base, handler = xnat_server
    seg = tmp_path / "s.dcm"
    seg.write_bytes(b"\0" * 128 + b"DICM" + b"rest")
    context = register.XnatContext(base, "alias", "secret", "TCIA-CPTAC-SAR_v9", "XNAT_E00950", "2")

    info = register.register_roi_collection(context, seg, "TotalSegmentator_scan2_x")

    assert info["status"] == 200 and info["label"] == "TotalSegmentator_scan2_x"
    call = handler.calls[0]
    assert call["path"] == "/xapi/roi/projects/TCIA-CPTAC-SAR_v9/sessions/XNAT_E00950/collections/TotalSegmentator_scan2_x?type=SEG&overwrite=true"
    assert call["auth"] == "Basic YWxpYXM6c2VjcmV0"
    assert call["length"] == 136 and call["first_bytes"] == b"DICM"


def test_register_raises_with_http_detail(xnat_server, tmp_path):
    base, handler = xnat_server
    handler.status_code = 403
    seg = tmp_path / "s.dcm"
    seg.write_bytes(b"x")
    context = register.XnatContext(base, "u", "p", "P", "S")
    with pytest.raises(RuntimeError, match="HTTP 403"):
        register.register_roi_collection(context, seg, "L")


def test_register_raises_when_host_unreachable(tmp_path):
    seg = tmp_path / "s.dcm"
    seg.write_bytes(b"x")
    context = register.XnatContext("http://127.0.0.1:9", "u", "p", "P", "S")
    with pytest.raises(RuntimeError, match="failed"):
        register.register_roi_collection(context, seg, "L", timeout_seconds=2)


def _wrapup_input_with_seg(tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    write_ct_series(inp / ".source_dicom")
    write_mask(inp / "mask.nii.gz", blob_mask(), affine=series_ras_affine())
    (inp / "labels.json").write_text(json.dumps({"3": "spleen", "7": "marker"}))
    return inp


def test_cli_registers_when_parent_context_present(xnat_server, tmp_path, monkeypatch):
    base, handler = xnat_server
    inp, out = _wrapup_input_with_seg(tmp_path), tmp_path / "out"
    for key, value in {"XNAT_HOST": base, "XNAT_USER": "u", "XNAT_PASS": "p", "SEG_PROJECT": "P1",
                       "SEG_SESSION_ID": "XNAT_E1", "SEG_SCAN_ID": "2"}.items():
        monkeypatch.setenv(key, value)

    assert cli.main(["--input", str(inp), "--output", str(out), "--model", "spleen_ct_segmentation",
                     "--keep-seg-file"]) == 0

    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["roi_collection"]["status"] == 200
    assert manifest["roi_collection"]["label"].startswith("spleen_ct_segmentation_scan2_")
    assert handler.calls[0]["path"].startswith("/xapi/roi/projects/P1/sessions/XNAT_E1/collections/spleen_ct_segmentation_scan2_")
    # --keep-seg-file, so the uploaded bytes can still be compared against the file on disk.
    assert handler.calls[0]["length"] == (out / "segmentation.seg.dcm").stat().st_size
    assert manifest["dicom_seg"]["retained_in_resource"] is True


def test_cli_registration_failure_is_logged_not_fatal(xnat_server, tmp_path, monkeypatch, caplog):
    base, handler = xnat_server
    handler.status_code = 500
    inp, out = _wrapup_input_with_seg(tmp_path), tmp_path / "out"
    for key, value in {"XNAT_HOST": base, "XNAT_USER": "u", "XNAT_PASS": "p", "SEG_PROJECT": "P1",
                       "SEG_SESSION_ID": "XNAT_E1"}.items():
        monkeypatch.setenv(key, value)

    assert cli.main(["--input", str(inp), "--output", str(out), "--roi-label", "custom_label"]) == 0
    assert "ROI collection not registered" in caplog.text
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["roi_collection"]["label"] == "custom_label" and "HTTP 500" in manifest["roi_collection"]["error"]
    assert (out / "segmentation.seg.dcm").exists()


def test_cli_skips_registration_without_context(tmp_path, monkeypatch, caplog):
    for key in ("XNAT_HOST", "XNAT_USER", "XNAT_PASS", "SEG_PROJECT", "SEG_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    inp, out = _wrapup_input_with_seg(tmp_path), tmp_path / "out"
    with caplog.at_level(logging.INFO):
        assert cli.main(["--input", str(inp), "--output", str(out)]) == 0
    assert "ROI registration skipped" in caplog.text
    assert json.loads((out / "wrapup.json").read_text())["roi_collection"] is None


def test_cli_no_register_flag(xnat_server, tmp_path, monkeypatch):
    base, handler = xnat_server
    inp, out = _wrapup_input_with_seg(tmp_path), tmp_path / "out"
    for key, value in {"XNAT_HOST": base, "XNAT_USER": "u", "XNAT_PASS": "p", "SEG_PROJECT": "P1",
                       "SEG_SESSION_ID": "XNAT_E1"}.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["--input", str(inp), "--output", str(out), "--no-register"]) == 0
    assert handler.calls == []


def test_cli_drops_seg_from_resource_once_the_collection_holds_it(xnat_server, tmp_path, monkeypatch):
    """The ROI collection stores a full copy, so the scan resource should not keep a second one."""
    base, handler = xnat_server
    inp, out = _wrapup_input_with_seg(tmp_path), tmp_path / "out"
    for key, value in {"XNAT_HOST": base, "XNAT_USER": "u", "XNAT_PASS": "p", "SEG_PROJECT": "P1",
                       "SEG_SESSION_ID": "XNAT_E1", "SEG_SCAN_ID": "2"}.items():
        monkeypatch.setenv(key, value)

    assert cli.main(["--input", str(inp), "--output", str(out), "--model", "demo"]) == 0

    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["roi_collection"]["status"] == 200
    assert manifest["dicom_seg"]["retained_in_resource"] is False
    assert not (out / "segmentation.seg.dcm").exists()
    # The collection still received the whole file: dropping happens after a successful PUT.
    assert handler.calls[0]["length"] > 0
    # Everything else the resource is for is untouched.
    assert {"volumes.json", "report.html", "wrapup.json"} <= {q.name for q in out.iterdir()}


def test_cli_keeps_seg_when_registration_is_skipped_by_flag(xnat_server, tmp_path, monkeypatch):
    """No collection means the SEG in the resource is the only copy, so it must survive."""
    base, handler = xnat_server
    inp, out = _wrapup_input_with_seg(tmp_path), tmp_path / "out"
    for key, value in {"XNAT_HOST": base, "XNAT_USER": "u", "XNAT_PASS": "p", "SEG_PROJECT": "P1",
                       "SEG_SESSION_ID": "XNAT_E1"}.items():
        monkeypatch.setenv(key, value)

    assert cli.main(["--input", str(inp), "--output", str(out), "--no-register"]) == 0

    assert handler.calls == []
    assert (out / "segmentation.seg.dcm").exists()
    assert json.loads((out / "wrapup.json").read_text())["dicom_seg"]["retained_in_resource"] is True


def test_cli_keeps_seg_when_there_is_no_xnat_context(tmp_path, monkeypatch):
    """Run outside XNAT: nothing registered the SEG anywhere, so it stays in the output."""
    for key in ("XNAT_HOST", "XNAT_USER", "XNAT_PASS", "SEG_PROJECT", "SEG_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    inp, out = _wrapup_input_with_seg(tmp_path), tmp_path / "out"

    assert cli.main(["--input", str(inp), "--output", str(out)]) == 0

    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["roi_collection"] is None
    assert manifest["dicom_seg"]["retained_in_resource"] is True
    assert (out / "segmentation.seg.dcm").exists()
