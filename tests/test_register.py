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
    # the fake answers no session lookup, so the label carries the session id (still unique per project)
    assert manifest["roi_collection"]["label"].startswith("spleen_ct_segmentation_XNAT_E1_scan2_")
    assert handler.calls[-1]["path"].startswith("/xapi/roi/projects/P1/sessions/XNAT_E1/collections/spleen_ct_segmentation_XNAT_E1_scan2_")
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


def test_collection_label_carries_the_session_and_trims_only_the_model():
    """Labels are unique per project: two sessions' runs of one pipeline finishing in the same
    second (Merlin on RSNA0001/RSNA0002, 2026-09-06) must not build the same label."""
    from datetime import datetime, timezone
    from segwrapup.register import LABEL_MAX, collection_label
    when = datetime(2026, 9, 6, 20, 11, 35, tzinfo=timezone.utc)
    a = collection_label("merlin", "2", when, session_label="RSNA0001")
    b = collection_label("merlin", "2", when, session_label="RSNA0002")
    assert a == "merlin_RSNA0001_scan2_20260906T201135Z" and a != b
    assert collection_label("merlin", "2", when) == "merlin_scan2_20260906T201135Z"          # no session: old shape
    long = collection_label("A" * 80, "2", when, session_label="RSNA260904145051_0002")
    assert len(long) <= LABEL_MAX and long.endswith("_RSNA260904145051_0002_scan2_20260906T201135Z")
    assert collection_label("!!!", "2", when, session_label="S/1") == "SEG_S_1_scan2_20260906T201135Z"
    # the reserve holds at the boundary: a 35-character scan id with reserve 7 must leave a 57-character
    # label at most, stamp intact, and a longer id gives up its own tail, never the stamp (round 15)
    for scan in ("s" * 35, "s" * 36, "s" * 60):
        label = collection_label("merlin", scan, when, session_label="RSNA0001", reserve=len("_record"))
        assert len(label) <= LABEL_MAX - len("_record"), (scan, label)
        assert label.endswith("_20260906T201135Z") and label.startswith("m")
    assert len(collection_label("merlin", "s" * 80, when)) <= LABEL_MAX


def test_long_scan_ids_and_session_labels_that_share_a_prefix_keep_distinct_labels():
    """A long scan id was cut to its prefix, so two scans of one session whose ids share it (series UIDs
    under one study root) got the same label in the same second, and register_collection's overwrite
    replaced the first scan's collection with the second's (Codex P2, PR #21 round 102). A shortened
    scan id or session label keeps a digest of the whole of it."""
    from datetime import datetime, timezone
    from segwrapup.register import LABEL_MAX, collection_label
    when = datetime(2026, 9, 30, 3, 5, 47, tzinfo=timezone.utc)
    root = "1.3.12.2.1107.5.2.43.66037.30000026093003054700000"
    scans = (root + "11", root + "12")
    for reserve in (0, len("_record")):
        a, b = (collection_label("merlin", scan, when, session_label="RSNA0001", reserve=reserve) for scan in scans)
        assert a != b, (reserve, a)
        for label in (a, b):
            assert len(label) <= LABEL_MAX - reserve and label.endswith("_20260930T030547Z"), label
        # the same scan always shortens the same way, so a label can be rebuilt
        assert a == collection_label("merlin", scans[0], when, session_label="RSNA0001", reserve=reserve)
    # two sessions whose long labels share the prefix that fits, in one project and one second
    sessions = ("RSNA_DEMO_COHORT_" + "X" * 40 + "_0001", "RSNA_DEMO_COHORT_" + "X" * 40 + "_0002")
    a, b = (collection_label("monailabel-train", "", when, session_label=owner, reserve=len("_record")) for owner in sessions)
    assert a != b and all(len(label) <= LABEL_MAX - len("_record") for label in (a, b)), (a, b)
    # the control: ids that fit are untouched
    assert collection_label("merlin", "2", when, session_label="RSNA0001") == "merlin_RSNA0001_scan2_20260930T030547Z"


def test_a_long_scan_id_leaves_room_for_the_session_that_tells_runs_apart():
    """A scan id too long to fit took all the room and the session label was dropped, so two sessions running
    one model on the same long scan id in the same second built the same label, which the project refuses
    and the ROI registration does not retry (Codex P2, PR #21 round 103). The session keeps its digest."""
    from datetime import datetime, timezone
    from segwrapup.register import LABEL_MAX, collection_label
    when = datetime(2026, 9, 30, 3, 31, 30, tzinfo=timezone.utc)
    scan = "1.3.12.2.1107.5.2.43.66037.30000026093003313000000011"
    for reserve in (0, len("_record")):
        for sessions in (("RSNA0001", "RSNA0002"),
                         ("RSNA_DEMO_COHORT_" + "X" * 40 + "_0001", "RSNA_DEMO_COHORT_" + "X" * 40 + "_0002")):
            a, b = (collection_label("merlin", scan, when, session_label=s, reserve=reserve) for s in sessions)
            assert a != b, (reserve, a)
            for label, session in zip((a, b), sessions):
                assert len(label) <= LABEL_MAX - reserve and label.endswith("_20260930T033130Z"), label
                assert "_scan1_3_12" in label, "the scan keeps a readable start too"
                if len(session) <= 16:
                    assert f"_{session}_" in label, "a short session label is kept whole"
    # the control: the same scan id and session in different seconds differ by the stamp, as before
    assert collection_label("merlin", scan, when, session_label="RSNA0001") != collection_label(
        "merlin", scan, when.replace(second=31), session_label="RSNA0001")


def test_a_host_without_a_scheme_is_a_best_effort_failure_not_an_abort(caplog):
    """A malformed XNAT_HOST (no scheme) makes urllib's Request raise ValueError, and the best-effort readers
    built their request before their guard, so fetch_dataset_facts aborted proc-wrapup before its report and
    manifest (Codex P2, PR #21 round 105). open_session, which auth_headers calls first, did the same, and so
    did the session and subject readers and close_session. Each falls back now, as for no answer at all."""
    from segwrapup.register import (XnatContext, close_session, fetch_dataset_facts, fetch_session_label,
                                    fetch_subject_label, open_session)
    def ctx(**kw):
        return XnatContext(host="demo02.xnatworks.io", user="u", password="p", project="P1", session="XNAT_E1", **kw)
    with caplog.at_level("WARNING"):
        assert open_session(ctx()) is False
        assert fetch_dataset_facts(ctx(dataset="XNAT_D1")) == {}
        assert fetch_session_label(ctx()) == "XNAT_E1"
        assert fetch_subject_label(ctx(subject="XNAT_S1")) == "XNAT_S1"
        closing = ctx()
        object.__setattr__(closing, "jsession", "ABCDEFGH12345678")
        close_session(closing)
        assert closing.jsession == ""
    assert "unknown url type" in caplog.text
    # the control: the same readers with no dataset make no request at all
    assert fetch_dataset_facts(ctx(dataset="")) == {}


def test_fetch_session_label_falls_back_to_the_id(caplog):
    from segwrapup.register import XnatContext, fetch_session_label
    context = XnatContext(host="http://127.0.0.1:9", user="u", password="p", project="P", session="XNAT_E1", session_tried=True)
    with caplog.at_level("WARNING"):
        assert fetch_session_label(context, timeout_seconds=1) == "XNAT_E1"
    assert "could not read the label of session XNAT_E1" in caplog.text


def test_from_env_subject_scope_needs_a_subject_when_there_is_no_session(caplog):
    from segwrapup import register
    base = {"XNAT_HOST": "http://x/", "XNAT_USER": "u", "XNAT_PASS": "p", "PROC_PROJECT": "P1"}
    with caplog.at_level("INFO"):
        assert register.XnatContext.from_env(base) is None
    assert "SEG_SESSION_ID (or SEG_SUBJECT_ID for a subject-scoped run, PROC_DATASET_ID for a dataset-scoped run)" in caplog.text
    context = register.XnatContext.from_env({**base, "PROC_SUBJECT_ID": "XNAT_S1"})
    assert context.scope == "subject" and context.subject == "XNAT_S1" and context.session == "" and context.target == "XNAT_S1"
    both = register.XnatContext.from_env({**base, "PROC_SUBJECT_ID": "XNAT_S1", "PROC_SESSION_ID": "XNAT_E1"})
    assert both.scope == "session" and both.target == "XNAT_E1"     # a session run keeps session scope even if a subject is named


def test_fetch_target_label_reads_the_subject_at_subject_scope_and_falls_back_to_the_id(caplog):
    import json, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from segwrapup.register import XnatContext, fetch_target_label
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps({"items": [{"data_fields": {"label": "292"}}]}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            self.send_response(500); self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        context = XnatContext(f"http://127.0.0.1:{server.server_port}", "u", "p", "P", "", subject="XNAT_S09007")
        assert fetch_target_label(context) == "292"
        assert seen[-1] == "/data/projects/P/subjects/XNAT_S09007?format=json"
    finally:
        server.shutdown()
    dead = XnatContext("http://127.0.0.1:9", "u", "p", "P", "", subject="XNAT_S09007")
    with caplog.at_level("WARNING"):
        assert fetch_target_label(dead) == "XNAT_S09007"
    assert "could not read the label of subject XNAT_S09007" in caplog.text


# ── dataset scope (0.7.0) ──────────────────────────────────────────────────────

def test_from_env_dataset_scope_needs_only_a_dataset_and_yields_to_a_subject_or_session():
    """A dataset-context wrapper sets PROC_DATASET_ID and nothing narrower; a run that also
    names a subject or a session keeps the narrower scope (docs/DATASET-SCOPE.md)."""
    from segwrapup import register
    base = {"XNAT_HOST": "http://x/", "XNAT_USER": "u", "XNAT_PASS": "p", "PROC_PROJECT": "P1"}
    context = register.XnatContext.from_env({**base, "PROC_DATASET_ID": "XNAT_D1"})
    assert context.scope == "dataset" and context.dataset == "XNAT_D1" and context.target == "XNAT_D1"
    assert context.session == "" and context.subject == ""
    assert register.XnatContext.from_env({**base, "SEG_DATASET_ID": " XNAT_D2 "}).dataset == "XNAT_D2"    # the SEG_ alias, stripped
    with_subject = register.XnatContext.from_env({**base, "PROC_DATASET_ID": "XNAT_D1", "PROC_SUBJECT_ID": "XNAT_S1"})
    assert with_subject.scope == "subject" and with_subject.target == "XNAT_S1"
    with_session = register.XnatContext.from_env({**base, "PROC_DATASET_ID": "XNAT_D1", "PROC_SESSION_ID": "XNAT_E1"})
    assert with_session.scope == "session" and with_session.target == "XNAT_E1"
    assert register.XnatContext(host="http://x", user="u", password="p", project="P1", session="").scope == "session"   # nothing named: the old default


def test_fetch_dataset_facts_reads_label_and_member_count_and_falls_back_to_the_id(caplog):
    import json, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from segwrapup.register import XnatContext, fetch_dataset_facts, fetch_dataset_label, fetch_target_label
    seen = []
    # XNAT's ?format=json shape (demo02 XNAT_E25423, 2026-09-24): the type is in ``meta``, the
    # fields in ``data_fields``; ``included_count`` arrives as a number.
    answers = {"XNAT_D1": ("analysis:analysisDatasetData", {"label": "cohort-v1", "included_count": 26}),
               "XNAT_D2": ("xnat:mrSessionData", {"label": "not-a-cohort"}),
               "XNAT_D3": ("analysis:analysisDatasetData", {"label": "odd", "included_count": "many"}),
               "XNAT_D5": ("analysis:analysisDatasetData", None),                 # valid JSON, data_fields null
               "XNAT_D6": ("analysis:analysisDatasetData", ["label", "cohort"]),  # or not an object
               "XNAT_D7": (["analysis:analysisDatasetData"], {"label": "listy", "included_count": 3}),   # meta of the wrong shape
               "XNAT_D8": ("analysis:analysisDatasetData", {"label": "huge", "included_count": 1e309}),   # decodes as infinity
               "XNAT_D9x": ("analysis:analysisDatasetData", {"label": "frac", "included_count": 2.5}),
               "XNAT_D10": ("analysis:analysisDatasetData", {"label": "whole", "included_count": 12.0}),
               # a JSON string holding an unpaired surrogate: Python decodes it, and every write of it
               # raises UnicodeEncodeError after the run (Codex P2, PR #21 round 33)
               "XNAT_D12": ("analysis:analysisDatasetData", {"label": "\ud800cohort", "included_count": 3})}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            asset = self.path.split("/experiments/")[-1].split("?")[0]
            if asset == "XNAT_D11":                                  # valid JSON nested past the parser's recursion limit (round 27)
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"items": [' + b"[" * 100000 + b"]" * 100000 + b"]}"); return
            if asset == "XNAT_D4":                                   # a body cut short: http.client.IncompleteRead
                self.send_response(200); self.send_header("Content-Length", "4096"); self.end_headers()
                self.wfile.write(b'{"items": [{"data_fi'); self.wfile.flush(); self.connection.close(); return
            if asset not in answers:
                self.send_response(500); self.end_headers(); return
            xsi, fields = answers[asset]
            meta = xsi if isinstance(xsi, list) else {"xsi:type": xsi, "isHistory": False}
            body = json.dumps({"items": [{"meta": meta, "data_fields": fields}]}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            self.send_response(500); self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host = f"http://127.0.0.1:{server.server_port}"
        def ctx(dataset):
            return XnatContext(host=host, user="u", password="p", project="P1", session="", dataset=dataset)
        assert fetch_dataset_facts(ctx("XNAT_D1")) == {"label": "cohort-v1", "included_count": 26}
        assert fetch_target_label(ctx("XNAT_D1")) == "cohort-v1"
        assert seen[-1] == "/data/experiments/XNAT_D1?format=json"
        with caplog.at_level("WARNING"):
            assert fetch_dataset_facts(ctx("XNAT_D2")) == {"label": "not-a-cohort"}
            assert fetch_dataset_facts(ctx("XNAT_D3")) == {"label": "odd"}
            assert fetch_dataset_label(ctx("XNAT_D9")) == "XNAT_D9"
            assert fetch_dataset_facts(ctx("XNAT_D4")) == {}, "a truncated answer is best-effort too (Codex P2, PR #21)"
            assert fetch_dataset_facts(ctx("XNAT_D11")) == {}, "nested past the recursion limit: best-effort, not an abort (round 27)"
            assert fetch_dataset_facts(ctx("XNAT_D5")) == {}, "data_fields null: best-effort, not AttributeError (Codex P2, round 10)"
            assert fetch_dataset_facts(ctx("XNAT_D6")) == {}
            assert fetch_dataset_facts(ctx("XNAT_D7")) == {"label": "listy", "included_count": 3}, "a meta of the wrong shape costs only the type check (round 11)"
            assert fetch_dataset_facts(ctx("XNAT_D8")) == {"label": "huge"}, "1e309 is infinity: no OverflowError out of the best-effort reader (round 14)"
            assert fetch_dataset_facts(ctx("XNAT_D9x")) == {"label": "frac"}
            assert fetch_dataset_facts(ctx("XNAT_D10")) == {"label": "whole", "included_count": 12}
            surrogate = fetch_dataset_facts(ctx("XNAT_D12"))
            assert surrogate == {"label": "\\ud800cohort", "included_count": 3}, "readable, and writable at all"
            surrogate["label"].encode()   # what the report, the record XML and wrapup.json all do
            assert fetch_dataset_label(ctx("XNAT_D12")) == "\\ud800cohort"
        from segwrapup.register import COUNT_MAX, _whole_count
        assert _whole_count(COUNT_MAX) == COUNT_MAX and _whole_count(str(COUNT_MAX)) == COUNT_MAX and _whole_count(float(2**31 - 1)) == COUNT_MAX
        for bad in (10 ** 400, str(10 ** 400), COUNT_MAX + 1, float(2**31), -1, True):   # a 400-digit literal is not a count (round 16)
            with pytest.raises((ValueError, TypeError)):
                _whole_count(bad)
        assert "meta of type list, not an object" in caplog.text
        assert "data_fields is NoneType, not an object" in caplog.text and "data_fields is list, not an object" in caplog.text
        assert "is a xnat:mrSessionData, not an analysis:analysisDatasetData" in caplog.text
        assert "non-numeric included_count" in caplog.text
        assert "could not read dataset XNAT_D9" in caplog.text
        assert "cannot be encoded as UTF-8" in caplog.text
        assert fetch_dataset_facts(XnatContext(host=host, user="u", password="p", project="P1", session="XNAT_E1")) == {}
    finally:
        server.shutdown()


def test_no_docstring_holds_an_unencodable_character():
    """A docstring with a lone surrogate in it compiles here but fails on Python 3.13 and later with
    "surrogates not allowed", so the module — and every CLI that imports it — cannot be imported at all
    there, while `pyproject.toml` accepts those versions (Codex P1, PR #21 round 34). A test string may
    legitimately hold one, and does; a docstring may not."""
    import ast, pathlib, segwrapup
    checked = 0
    for path in sorted(pathlib.Path(segwrapup.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node)
                if doc:
                    checked += 1
                    try:
                        doc.encode()
                    except UnicodeEncodeError as error:
                        raise AssertionError(f"{path.name}: a docstring cannot be encoded as UTF-8 "
                                             f"({error}); it will not compile on Python 3.13") from error
    assert checked > 50, f"only {checked} docstrings were checked; the walk is not finding them"


def test_collection_label_reserves_room_for_a_caller_suffix():
    """proc-wrapup appends _record; without the reservation a long dataset label made a 71-character
    record label that XNAT refuses (Codex P2, PR #21)."""
    from datetime import datetime, timezone
    from segwrapup.register import LABEL_MAX, collection_label
    when = datetime(2026, 9, 26, 17, 41, 42, tzinfo=timezone.utc)
    owner = "nnunet-nnunet_msd_spleen_demo-20260803_121930-with-a-very-long-cohort-name"
    plain = collection_label("monailabel-train", "", when=when, session_label=owner)
    assert len(plain) == LABEL_MAX
    reserved = collection_label("monailabel-train", "", when=when, session_label=owner, reserve=len("_record"))
    assert len(reserved + "_record") <= LABEL_MAX
    assert reserved.endswith("20260926T174142Z"), "the stamp that makes it unique is kept; the model name gives way"

