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
    DEFAULT_RESOURCES, DERIVED_ROLE, XSI_TYPE, RecordContract, build_record_xml, collect_files, publish_record,
    upload_format,
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
    existing_labels: set = set()      # GET .../assessors/<label> answers 200 for these
    get_status: int | None = None     # when set, every GET answers this instead

    def do_GET(self):
        _Handler.calls.append({"path": self.path, "method": "GET", "auth": self.headers.get("Authorization")})
        label = self.path.split("/assessors/")[-1].split("?")[0]
        self.send_response(_Handler.get_status or (200 if label in _Handler.existing_labels else 404))
        self.end_headers()

    def do_DELETE(self):
        _Handler.calls.append({"path": self.path, "method": "DELETE", "auth": self.headers.get("Authorization")})
        self.send_response(200)
        self.end_headers()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _Handler.calls.append({"path": self.path, "method": "PUT", "auth": self.headers.get("Authorization"),
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
    _Handler.calls, _Handler.fail_paths, _Handler.existing_labels, _Handler.get_status = [], set(), set(), None
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


def test_contract_from_discrete_variables_the_container_service_can_store():
    # CS caps each environment value at 255 chars, so the installer sets XNW_* variables.
    env = {"XNW_CARD_ID": "deepwmh", "XNW_CARD_REVISION": "1.1.0", "XNW_CONTAINER_DIGEST": "sha256:" + "b" * 64,
           "XNW_OUTPUT_RESOURCE_LABEL": "DEEPWMH", "XNW_RESOURCE_LOGS": "run.log, stderr.txt"}
    contract = RecordContract.from_env(env)
    assert contract.card_id == "deepwmh" and contract.card_revision == "1.1.0"
    assert contract.container_digest.endswith("b" * 64) and contract.output_resource_label == "DEEPWMH"
    assert contract.analysis_type == "segmentation"                  # default kept
    assert contract.resources["LOGS"] == ["run.log", "stderr.txt"]   # extra role from XNW_RESOURCE_LOGS
    assert contract.resources["METRICS"] == DEFAULT_RESOURCES["METRICS"]
    assert all(len(v) <= 255 for v in env.values())


def test_json_contract_wins_over_discrete_variables():
    env = {"XNW_CONTRACT": json.dumps({"card_id": "from-json"}), "XNW_CARD_ID": "from-discrete"}
    assert RecordContract.from_env(env).card_id == "from-json"


@pytest.mark.parametrize("raw", ["not json", "[1,2]", json.dumps({"resources": {"METRICS": "volumes.json"}})])
def test_contract_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        RecordContract.from_env({"XNW_CONTRACT": raw})


# ── files and XML ──────────────────────────────────────────────────────────────

def test_collect_files_named_roles_then_everything_else_is_derived(tmp_path):
    for name in ("volumes.json", "report.html", "wrapup.json", "segmentation.nii.gz", "segmentation_uint8.nii.gz"):
        (tmp_path / name).write_text("x")
    (tmp_path / "meshes").mkdir()
    (tmp_path / "meshes" / "liver.stl").write_text("x")
    (tmp_path / ".source_dicom").mkdir()
    (tmp_path / ".source_dicom" / "1.dcm").write_text("x")     # hidden: never uploaded
    (tmp_path / ".DS_Store").write_text("x")
    files = collect_files(tmp_path, RecordContract())
    assert {role: [p.relative_to(tmp_path).as_posix() for p in paths] for role, paths in files.items()} == {
        "METRICS": ["volumes.json"], "REPORT": ["report.html"], "PROVENANCE": ["wrapup.json"],
        DERIVED_ROLE: ["meshes/liver.stl", "segmentation.nii.gz", "segmentation_uint8.nii.gz"]}
    # the whole output is on the record, each file in exactly one role
    every = [p for paths in files.values() for p in paths]
    assert len(every) == len(set(every)) == 6


def test_collect_files_explicit_derived_globs_replace_the_everything_else_default(tmp_path):
    for name in ("volumes.json", "segmentation.nii.gz", "scratch.bin"):
        (tmp_path / name).write_text("x")
    contract = RecordContract.from_env({"XNW_CARD_ID": "c", "XNW_RESOURCE_DERIVED": "*.nii.gz"})
    files = collect_files(tmp_path, contract)
    assert [p.name for p in files[DERIVED_ROLE]] == ["segmentation.nii.gz"]
    assert "scratch.bin" not in str(files)


def test_collect_files_overlapping_role_globs_assign_each_file_once(tmp_path):
    """Codex P2 on PR #3: METRICS overridden to *.json also matches wrapup.json, which the default
    PROVENANCE pattern names too. First role wins; nothing is uploaded twice."""
    for name in ("volumes.json", "wrapup.json", "report.html"):
        (tmp_path / name).write_text("x")
    contract = RecordContract.from_env({"XNW_CARD_ID": "c", "XNW_RESOURCE_METRICS": "*.json"})
    files = collect_files(tmp_path, contract)
    assert [p.name for p in files["METRICS"]] == ["volumes.json", "wrapup.json"]
    assert "PROVENANCE" not in files
    every = [p for paths in files.values() for p in paths]
    assert len(every) == len(set(every)) == 3


@pytest.mark.parametrize("name,fmt", [("a.nii.gz", "NIFTI"), ("a.nii", "NIFTI"), ("a.seg.dcm", "DICOM"),
                                      ("volumes.json", "JSON"), ("x.stl", "STL"), ("README", "FILE"),
                                      ("artifact.QC result", "QCRESULT"), ("x.a?b#c", "ABC"), ("odd. ", "FILE")])
def test_upload_format_by_suffix(name, fmt):
    assert upload_format(Path(name)) == fmt


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
                     "<analysis:output_resource_label>DEEPWMH<", "<analysis:output_file_count>1<",  # one file in tmp_path
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
    (tmp_path / "segmentation.nii.gz").write_bytes(b"\x1f\x8b")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "part.tsv").write_text("a\tb")
    files = collect_files(tmp_path, RecordContract())
    outcome = publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", files, output_dir=tmp_path)
    assert outcome["id"] == "XNAT_E99999" and outcome["xsi_type"] == XSI_TYPE
    paths = [c["path"] for c in handler.calls]
    # create-only: existence is checked first, then the create PUT
    assert paths[0] == "/data/experiments/XNAT_E00018/assessors/DeepWMH_scan2_X?format=json" and handler.calls[0]["method"] == "GET"
    assert paths[1] == "/data/experiments/XNAT_E00018/assessors/DeepWMH_scan2_X?inbody=true"
    assert handler.calls[1]["content_type"] == "application/xml" and handler.calls[1]["body"] == b"<xml/>"
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/METRICS/files/volumes.json?inbody=true&format=JSON" in paths
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/REPORT/files/report.html?inbody=true&format=HTML" in paths
    # the data output rides along under DERIVED, nested paths kept, NIfTI declared as NIFTI
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/DERIVED/files/segmentation.nii.gz?inbody=true&format=NIFTI" in paths
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/DERIVED/files/sub/part.tsv?inbody=true&format=TSV" in paths
    assert all(c["auth"].startswith("Basic ") for c in handler.calls)
    assert outcome["uploaded"] == {"METRICS": ["volumes.json"], "REPORT": ["report.html"],
                                   "DERIVED": ["segmentation.nii.gz", "sub/part.tsv"]}


def test_publish_refuses_a_label_that_already_exists(xnat, tmp_path):
    """Codex P1 on PR #3: a reused --record-label would turn the create PUT into an update."""
    host, handler = xnat
    handler.existing_labels = {"DeepWMH_scan2_X"}
    with pytest.raises(RuntimeError, match="already exists"):
        publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", {})
    assert [c["method"] for c in handler.calls] == ["GET"], "nothing is written when the label exists"


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_publish_refuses_when_the_existence_check_is_inconclusive(xnat, tmp_path, status):
    """Codex round 4 on PR #3: anything but 404 must not fall through to a create-or-update PUT."""
    host, handler = xnat
    handler.get_status = status
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", {})
    assert [c["method"] for c in handler.calls] == ["GET"]


def test_probe_protocol_error_is_a_runtime_error_the_guard_records(xnat, tmp_path, monkeypatch):
    """Codex round 6 on PR #3: a malformed response to the existence probe escaped as HTTPException."""
    import http.client
    import segwrapup.publish as publish
    host, handler = xnat

    def urlopen_bad_status(request, timeout=None):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(publish.urllib.request, "urlopen", urlopen_bad_status)
    with pytest.raises(RuntimeError, match=r"GET .*assessors/DeepWMH_scan2_X failed: garbage"):
        publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", {})


def test_publish_deletes_the_record_when_a_file_upload_fails(xnat, tmp_path):
    """Codex P1 on PR #3: a create followed by a failed upload left a SUCCEEDED-looking partial record."""
    host, handler = xnat
    (tmp_path / "volumes.json").write_text("{}")
    (tmp_path / "report.html").write_text("<html/>")
    handler.fail_paths = {"/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/REPORT/"}
    with pytest.raises(RuntimeError, match=r"HTTP 500 boom; record XNAT_E99999 deleted \(HTTP 200\)"):
        publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", collect_files(tmp_path, RecordContract()))
    deletes = [c for c in handler.calls if c["method"] == "DELETE"]
    assert [c["path"] for c in deletes] == ["/data/experiments/XNAT_E00018/assessors/XNAT_E99999?removeFiles=true"]


def test_publish_rolls_back_when_a_collected_file_vanished(xnat, tmp_path):
    """Codex round 3 on PR #3: read_bytes() raising OSError skipped the rollback."""
    host, handler = xnat
    (tmp_path / "volumes.json").write_text("{}")
    (tmp_path / "report.html").write_text("<html/>")
    files = collect_files(tmp_path, RecordContract())
    (tmp_path / "report.html").unlink()          # gone between collection and upload
    with pytest.raises(RuntimeError, match=r"report.html.*record XNAT_E99999 deleted"):
        publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", files)
    assert [c["path"] for c in handler.calls if c["method"] == "DELETE"] == \
        ["/data/experiments/XNAT_E00018/assessors/XNAT_E99999?removeFiles=true"]


@pytest.mark.parametrize("role", ["QC RESULTS", "a/b", "x?y", "#tag", ""])
def test_contract_rejects_unsafe_resource_roles(role):
    """Codex round 3 on PR #3: a role with a space reached urllib as a raw path segment."""
    with pytest.raises(ValueError, match="not a valid XNAT resource label"):
        RecordContract.from_env({"XNW_CONTRACT": json.dumps({**CONTRACT, "resources": {role: ["*.json"]}})})
    with pytest.raises(ValueError, match="not a valid XNAT resource label"):
        RecordContract.from_env({"XNW_CARD_ID": "c", "XNW_RESOURCE_" + role: "*.json"})


def test_contract_accepts_lowercase_roles_by_uppercasing():
    contract = RecordContract.from_env({"XNW_CARD_ID": "c", "XNW_RESOURCE_logs": "*.log"})
    assert contract.resources["LOGS"] == ["*.log"]


def test_publish_rolls_back_on_an_invalid_upload_url(xnat, tmp_path, monkeypatch):
    """Codex round 5 on PR #3: urllib's InvalidURL is a ValueError and skipped the rollback."""
    host, handler = xnat
    (tmp_path / "volumes.json").write_text("{}")
    files = collect_files(tmp_path, RecordContract())
    import segwrapup.publish as publish
    real_put = publish._put

    def put_raising_invalid_url(context, url, body, content_type, timeout):
        if "/out/" in url:
            import http.client
            raise http.client.InvalidURL("URL can't contain control characters")   # a ValueError subclass
        return real_put(context, url, body, content_type, timeout)

    monkeypatch.setattr(publish, "_put", put_raising_invalid_url)
    with pytest.raises(RuntimeError, match=r"control characters; record XNAT_E99999 deleted"):
        publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", files)
    assert [c["method"] for c in handler.calls][-1] == "DELETE"


def test_record_xml_warns_when_a_delivered_mask_could_not_be_measured():
    """Codex P1 on PR #3: one good mask plus one unmeasurable mask must not be auto QC PASS."""
    xml = build_record_xml(_context("http://x"), RecordContract(), "L", _report(), _results(), {},
                           source_dicom_present=True, unmeasured_masks=1)
    assert "<analysis:auto_qc_status>WARN<" in xml and '"unmeasured_masks": 1' in xml
    xml = build_record_xml(_context("http://x"), RecordContract(), "L", _report(), _results(), {},
                           source_dicom_present=True, unmeasured_masks=0)
    assert "<analysis:auto_qc_status>PASS<" in xml


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
    for key in list(CONTEXT_ENV) + ["XNW_CONTRACT", "SEG_NO_REGISTER", "SEG_NO_PUBLISH"] + list(RecordContract.DISCRETE_KEYS):
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
    create = next(c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"])
    assert create["path"].endswith(f"/assessors/{record['label']}?inbody=true")
    assert b"<analysis:pipeline_name>DeepWMH</analysis:pipeline_name>" in create["body"]
    assert b"<analysis:card_id>deepwmh</analysis:card_id>" in create["body"]


def test_cli_record_shares_the_roi_collection_label(xnat, tmp_path, monkeypatch):
    """One run, one stamp: the record is the ROI collection's sibling, with a _record suffix."""
    host, handler = xnat
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    write_ct_series(inp / ".source_dicom")
    write_mask(inp / "segmentation.nii.gz", blob_mask(), affine=series_ras_affine())
    for key, value in {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CONTRACT": json.dumps(CONTRACT)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SEG_NO_REGISTER", raising=False)
    assert cli.main(["--input", str(inp), "--output", str(out), "--model", "DeepWMH", "--model-version", "1.0.1"]) == 0
    manifest = json.loads((out / "wrapup.json").read_text())
    # Same stamp, distinct label: identical labels collide (labels are unique per project
    # across experiment types) and XNAT answers 417 "Invalid character in experiment label".
    assert manifest["analysis_record"]["label"] == manifest["roi_collection"]["label"] + "_record"
    roi_puts = [c for c in handler.calls if "/xapi/roi/" in c["path"]]
    record_puts = [c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"]]
    assert len(roi_puts) == 1 and len(record_puts) == 1


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


def test_cli_bad_contract_glob_is_recorded_not_fatal(xnat, tmp_path, monkeypatch, caplog):
    """Codex P1 on PR #3: an absolute glob makes Path.glob raise NotImplementedError; that must
    land in wrapup.json as the record's error, with the masks still delivered."""
    host, handler = xnat
    caplog.set_level(logging.ERROR, logger="segwrapup.publish")
    code, manifest, out = _run_with_masks(tmp_path, monkeypatch, {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CARD_ID": "deepwmh",
                                                                  "XNW_RESOURCE_METRICS": "/output/*.json"})
    assert code == 0
    assert (out / "segmentation.nii.gz").exists()
    assert manifest["analysis_record"]["error"].startswith("NotImplementedError")
    assert not [c for c in handler.calls if "/assessors/" in c["path"]], "nothing was PUT for the record"
    assert "not published" in caplog.text


def test_cli_no_publish_flag(xnat, tmp_path, monkeypatch):
    host, handler = xnat
    env = {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CONTRACT": json.dumps(CONTRACT)}
    code, manifest, _ = _run_with_masks(tmp_path, monkeypatch, env, "--no-publish")
    assert code == 0 and manifest["analysis_record"] is None and handler.calls == []
