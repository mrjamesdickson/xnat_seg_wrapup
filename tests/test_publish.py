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
from html import unescape as html_unescape

from segwrapup.publish import (
    DEFAULT_RESOURCES, DERIVED_ROLE, XSI_TYPE, RecordContract, build_record_xml, collect_files, collect_views,
    publish_record, upload_format,
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

    def do_POST(self):
        _Handler.calls.append({"path": self.path, "method": "POST", "auth": self.headers.get("Authorization"), "cookie": self.headers.get("Cookie")})
        if self.path == "/data/JSESSION" and "/data/JSESSION" not in _Handler.fail_paths:
            self.send_response(200); self.end_headers(); self.wfile.write(b"FAKESESSION1234")
        else:
            self.send_response(500); self.end_headers()

    conflict_labels: set = set()      # PUT create of these labels answers 409 (taken elsewhere in the project)

    def do_GET(self):
        _Handler.calls.append({"path": self.path, "method": "GET", "auth": self.headers.get("Authorization"), "cookie": self.headers.get("Cookie")})
        if "/subjects/" in self.path and "/experiments/" in self.path and self.path.endswith("?format=json"):
            # a subject-scope label probe: /data/projects/P/subjects/S/experiments/<label>
            label = self.path.split("/experiments/")[-1].split("?")[0]
            self.send_response(_Handler.get_status or (200 if label in _Handler.existing_labels else 404))
            self.end_headers()
            return
        if "/assessors/" not in self.path and self.path.endswith("?format=json"):
            body = json.dumps({"items": [{"data_fields": {"label": "SUBJ01" if "/subjects/" in self.path else "SESS01"}}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
            return
        label = self.path.split("/assessors/")[-1].split("?")[0]
        self.send_response(_Handler.get_status or (200 if label in _Handler.existing_labels else 404))
        self.end_headers()

    def do_DELETE(self):
        _Handler.calls.append({"path": self.path, "method": "DELETE", "auth": self.headers.get("Authorization"), "cookie": self.headers.get("Cookie")})
        self.send_response(200)
        self.end_headers()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _Handler.calls.append({"path": self.path, "method": "PUT", "auth": self.headers.get("Authorization"),
                               "cookie": self.headers.get("Cookie"), "content_type": self.headers.get("Content-Type"), "body": body})
        if any(self.path.startswith(p) for p in _Handler.fail_paths):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        if "/assessors/" in self.path and "/out/" not in self.path and (_Handler.conflict_labels is None or self.path.split("/assessors/")[1].split("?")[0] in _Handler.conflict_labels):
            self.send_response(409); self.end_headers(); self.wfile.write(b"<h3>Conflict: Duplicate experiment label</h3>"); return
        is_create = ("/assessors/" in self.path and "/out/" not in self.path) or ("/subjects/" in self.path and "/experiments/" in self.path)
        self.send_response(201 if is_create else 200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        # XNAT answers an assessor (or subject assessor) create with the new accession ID as plain text.
        self.wfile.write(b"XNAT_E99999" if is_create else b"")

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


def test_contract_parses_fields_and_resource_overrides(caplog):
    env = {"XNW_CONTRACT": json.dumps({**CONTRACT, "resources": {"metrics": ["*.json"], "LOGS": ["*.log"]}})}
    with caplog.at_level(logging.WARNING):
        contract = RecordContract.from_env(env)
    assert contract.card_id == "deepwmh" and contract.container_digest.startswith("sha256:")
    assert contract.resources["METRICS"] == ["*.json"]          # a view role: override taken, upper-cased
    assert "LOGS" not in contract.resources                     # a fixed resource: the wrapup decides, the card's globs are ignored
    assert contract.ignored_overrides == ("LOGS=*.log",) and "LOGS is a fixed resource" in caplog.text
    assert contract.resources["REPORT"] == ["report.html"]      # default kept


def test_contract_from_discrete_variables_the_container_service_can_store(caplog):
    # CS caps each environment value at 255 chars, so the installer sets XNW_* variables.
    env = {"XNW_CARD_ID": "deepwmh", "XNW_CARD_REVISION": "1.1.0", "XNW_CONTAINER_DIGEST": "sha256:" + "b" * 64,
           "XNW_OUTPUT_RESOURCE_LABEL": "DEEPWMH", "XNW_RESOURCE_LOGS": "run.log, stderr.txt", "XNW_RESOURCE_QC": "raw/*_qc.tsv"}
    with caplog.at_level(logging.WARNING):
        contract = RecordContract.from_env(env)
    assert contract.card_id == "deepwmh" and contract.card_revision == "1.1.0"
    assert contract.container_digest.endswith("b" * 64) and contract.output_resource_label == "DEEPWMH"
    assert contract.analysis_type == "segmentation"                  # default kept
    assert contract.resources["QC"] == ["raw/*_qc.tsv"]              # a new view role from XNW_RESOURCE_QC
    assert "LOGS" not in contract.resources and contract.ignored_overrides == ("LOGS=run.log,stderr.txt",)   # fixed: ignored
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

def _names(files):
    return {role: [f.name for f in items] for role, items in files.items()}


def test_collect_files_wrapup_artefacts_by_role_then_everything_else_is_derived_and_metrics_is_a_view(tmp_path):
    """seg-wrapup's record since 0.6.0: REPORT and PROVENANCE hold the wrapup's own files, DERIVED
    everything else (masks, sidecars, the measurements), and METRICS is a view onto DERIVED."""
    for name in ("volumes.json", "report.html", "wrapup.json", "segmentation.nii.gz", "segmentation_uint8.nii.gz"):
        (tmp_path / name).write_text("x")
    (tmp_path / "meshes").mkdir()
    (tmp_path / "meshes" / "liver.stl").write_text("x")
    (tmp_path / ".source_dicom").mkdir()
    (tmp_path / ".source_dicom" / "1.dcm").write_text("x")     # the DICOM copy XNAT already holds: never uploaded
    (tmp_path / ".DS_Store").write_text("x")                    # any other dot entry is output, not the wrapup's to judge (0.6.1)
    files = collect_files(tmp_path, RecordContract())
    assert _names(files) == {
        "REPORT": ["report.html"], "PROVENANCE": ["wrapup.json"],
        DERIVED_ROLE: [".DS_Store", "meshes/liver.stl", "segmentation.nii.gz", "segmentation_uint8.nii.gz", "volumes.json"]}
    assert "METRICS" not in files                                     # not a resource any more
    assert collect_views(tmp_path, RecordContract(), files[DERIVED_ROLE]) == {"METRICS": ["volumes.json"]}
    # the whole output is on the record, each file in exactly one resource
    every = [f.path for items in files.values() for f in items]
    assert len(every) == len(set(every)) == 7


def test_collect_files_a_derived_override_is_ignored_the_tree_is_always_whole(tmp_path, caplog):
    """Until 0.5.0 XNW_RESOURCE_DERIVED replaced the everything-else default and left files off the
    record. Plan D20: DERIVED is always the complete tool output; the card's globs are ignored and said so."""
    for name in ("volumes.json", "segmentation.nii.gz", "scratch.bin"):
        (tmp_path / name).write_text("x")
    with caplog.at_level(logging.WARNING):
        contract = RecordContract.from_env({"XNW_CARD_ID": "c", "XNW_RESOURCE_DERIVED": "*.nii.gz"})
    files = collect_files(tmp_path, contract)
    assert _names(files)[DERIVED_ROLE] == ["scratch.bin", "segmentation.nii.gz", "volumes.json"]
    assert contract.ignored_overrides == ("DERIVED=*.nii.gz",) and "DERIVED is a fixed resource" in caplog.text


def test_collect_files_overlapping_role_globs_assign_each_file_once(tmp_path):
    """Codex P2 on PR #3: METRICS overridden to *.json also matches wrapup.json, which the default
    PROVENANCE pattern names too. Since 0.6.0 METRICS is a view onto DERIVED, so it can name
    volumes.json (on DERIVED) but never wrapup.json (on PROVENANCE); nothing is uploaded twice."""
    for name in ("volumes.json", "wrapup.json", "report.html"):
        (tmp_path / name).write_text("x")
    contract = RecordContract.from_env({"XNW_CARD_ID": "c", "XNW_RESOURCE_METRICS": "*.json"})
    files = collect_files(tmp_path, contract)
    assert _names(files) == {"REPORT": ["report.html"], "PROVENANCE": ["wrapup.json"], DERIVED_ROLE: ["volumes.json"]}
    assert collect_views(tmp_path, contract, files[DERIVED_ROLE]) == {"METRICS": ["volumes.json"]}
    every = [f.path for items in files.values() for f in items]
    assert len(every) == len(set(every)) == 3


def _tool_tree(root, derived="raw"):
    """A proc-wrapup output: the tool's derivatives dataset under raw/ (an HTML report whose
    figures are siblings, a JSON metric, a TSV), the wrapup's artefacts at the root."""
    tree = root / derived
    (tree / "sub-H025" / "figures").mkdir(parents=True)
    (tree / "sub-H025" / "anat").mkdir()
    (tree / "dataset_description.json").write_text('{"Name": "x"}')
    (tree / "sub-H025.html").write_text('<img src="sub-H025/figures/a.svg">')
    (tree / "sub-H025" / "figures" / "a.svg").write_text("<svg/>")
    (tree / "sub-H025" / "anat" / "sub-H025_T1w.json").write_text('{"cjv": 0.4}')
    (tree / "sub-H025" / "anat" / "sub-H025_desc-conf_timeseries.tsv").write_text("a\tb")
    (root / "report.html").write_text("<html/>")
    (root / "wrapup.json").write_text("{}")
    (root / "status.json").write_text('{"exit_code": 0}')
    (root / "logs").mkdir()
    (root / "logs" / "stdout.log").write_text("ran")
    return tree


PROC_DEFAULTS = {"REPORT": ["report.html"], "PROVENANCE": ["wrapup.json", "status.json", "prereq.json"], "LOGS": ["logs/*.log"]}


def test_derived_is_the_whole_tool_tree_at_the_resource_root_and_named_roles_are_views(tmp_path, caplog):
    """Plan D20 (James: "raw/sub-H025.html should be in with everything else"; "DERIVED should
    contain the entire output of the container"). On 0.5.0 mriqc E25614 had its reports in
    REPORT, its IQM JSONs in METRICS and 49 other files in DERIVED: three resources, no dataset."""
    _tool_tree(tmp_path)
    with caplog.at_level(logging.WARNING):
        contract = RecordContract.from_env({"XNW_CARD_ID": "mriqc", "XNW_RESOURCE_METRICS": "sub-*/**/*.json",
                                            "XNW_RESOURCE_REPORT": "report.html,sub-*.html"}, defaults=PROC_DEFAULTS)
    files = collect_files(tmp_path, contract, derived_root="raw")
    assert _names(files) == {
        "REPORT": ["report.html"], "PROVENANCE": ["wrapup.json", "status.json"], "LOGS": ["logs/stdout.log"],
        # the dataset, complete, at the root of the resource: no raw/ prefix, layout untouched
        DERIVED_ROLE: ["dataset_description.json", "sub-H025/anat/sub-H025_T1w.json", "sub-H025/anat/sub-H025_desc-conf_timeseries.tsv",
                       "sub-H025/figures/a.svg", "sub-H025.html"]}
    assert all(f.path == tmp_path / "raw" / f.name for f in files[DERIVED_ROLE])
    # METRICS is not a resource: it is a mapping onto DERIVED paths
    assert "METRICS" not in files
    assert collect_views(tmp_path, contract, files[DERIVED_ROLE], derived_root="raw") == {"METRICS": ["sub-H025/anat/sub-H025_T1w.json"]}
    # the card's REPORT override is not honoured: nothing the tool wrote is copied out of DERIVED
    assert contract.ignored_overrides == ("REPORT=report.html,sub-*.html",)


def test_a_tool_html_report_with_sibling_figures_stays_in_derived_and_is_not_copied_into_report(tmp_path):
    """fmriprep E25617 on 0.5.0: raw/sub-H025.html alone in REPORT rendered without its figures,
    which are relative links into sub-H025/figures/ (in DERIVED). The report lives with its figures."""
    _tool_tree(tmp_path)
    contract = RecordContract.from_env({"XNW_CARD_ID": "fmriprep", "XNW_RESOURCE_REPORT": "report.html,sub-*.html"}, defaults=PROC_DEFAULTS)
    files = collect_files(tmp_path, contract, derived_root="raw")
    assert _names(files)["REPORT"] == ["report.html"]
    assert {"sub-H025.html", "sub-H025/figures/a.svg"} <= set(_names(files)[DERIVED_ROLE])
    uploads = [(role, f.name) for role, items in files.items() for f in items]
    assert uploads.count(("REPORT", "sub-H025.html")) == 0 and len([u for u in uploads if u[1] == "sub-H025.html"]) == 1


def test_collect_files_keeps_the_datasets_dotfiles_and_skips_only_the_dicom_copy(tmp_path):
    """0.6.0 dropped every dot-prefixed entry from DERIVED, so a dataset's own dotfiles never reached
    the record: qsirecon's .bidsignore (the reference QSIRECON on demo02, XNAT_E09349, carries one;
    qsiprep and fmriprep write it too), heudiconv's .heudiconv/ directory. D20: DERIVED is the
    scientists' dataset byte-for-byte and path-for-path; the wrapup leaves out the one entry it
    reserves by name (the DICOM copy XNAT already holds) and nothing else."""
    tree = _tool_tree(tmp_path)
    (tree / ".bidsignore").write_text("*.html\n")
    (tree / ".heudiconv" / "sub-H025" / "info").mkdir(parents=True)
    (tree / ".heudiconv" / "sub-H025" / "info" / "dicominfo.tsv").write_text("series\n")
    (tree / "sub-H025" / ".qsirecon_state").write_text("done")
    (tree / ".source_dicom").mkdir()
    (tree / ".source_dicom" / "1.dcm").write_text("x")          # the card's DICOM copy at the tree root
    (tmp_path / ".source_dicom").mkdir()
    (tmp_path / ".source_dicom" / "1.dcm").write_text("x")      # ... or at the wrapup's output root
    contract = RecordContract.from_env({"XNW_CARD_ID": "qsirecon", "XNW_RESOURCE_METRICS": "**/*.tsv"}, defaults=PROC_DEFAULTS)
    files = collect_files(tmp_path, contract, derived_root="raw")
    derived = _names(files)[DERIVED_ROLE]
    assert derived[:2] == [".bidsignore", ".heudiconv/sub-H025/info/dicominfo.tsv"] and "sub-H025/.qsirecon_state" in derived
    assert not [name for items in _names(files).values() for name in items if ".source_dicom" in name]
    assert (tmp_path / ".source_dicom" / "1.dcm").exists() and (tree / ".source_dicom" / "1.dcm").exists()   # left alone, just not uploaded
    # a view glob reaches a dotfile like any other DERIVED file
    assert collect_views(tmp_path, contract, files[DERIVED_ROLE], derived_root="raw") == {
        "METRICS": [".heudiconv/sub-H025/info/dicominfo.tsv", "sub-H025/anat/sub-H025_desc-conf_timeseries.tsv"]}


def test_collect_files_without_a_derived_root_keeps_the_tools_dotfiles_under_raw(tmp_path):
    """seg-wrapup's shape: the tool's tree sits under raw/ in the wrapup's own output and keeps that
    prefix on the record (docs/ROLES-AS-VIEWS.md); its dotfiles come along, the DICOM copy does not."""
    (tmp_path / "segmentation.nii.gz").write_text("x")
    (tmp_path / "raw" / "stats").mkdir(parents=True)
    (tmp_path / "raw" / ".bidsignore").write_text("*.html\n")
    (tmp_path / "raw" / "stats" / ".cache").write_text("k")
    (tmp_path / "raw" / "stats" / "statistics.json").write_text("{}")
    (tmp_path / ".source_dicom").mkdir()
    (tmp_path / ".source_dicom" / "1.dcm").write_text("x")
    files = collect_files(tmp_path, RecordContract())
    assert _names(files) == {DERIVED_ROLE: ["raw/.bidsignore", "raw/stats/.cache", "raw/stats/statistics.json", "segmentation.nii.gz"]}


def test_a_legacy_raw_prefixed_view_glob_is_rebased_onto_the_derived_root_with_a_warning(tmp_path, caplog):
    """Cards pinned to 0.5.0 wrote METRICS globs as raw/sub-*/**/*.json; the same files match after the re-pin."""
    _tool_tree(tmp_path)
    contract = RecordContract.from_env({"XNW_CARD_ID": "mriqc", "XNW_RESOURCE_METRICS": "raw/sub-*/**/*.json"}, defaults=PROC_DEFAULTS)
    files = collect_files(tmp_path, contract, derived_root="raw")
    with caplog.at_level(logging.WARNING):
        views = collect_views(tmp_path, contract, files[DERIVED_ROLE], derived_root="raw")
    assert views == {"METRICS": ["sub-H025/anat/sub-H025_T1w.json"]}
    assert "drop the raw/ prefix" in caplog.text


def test_a_view_that_matches_nothing_is_recorded_empty_and_a_view_never_reaches_a_wrapup_artefact(tmp_path, caplog):
    _tool_tree(tmp_path)
    contract = RecordContract.from_env({"XNW_CARD_ID": "c", "XNW_RESOURCE_METRICS": "**/*.csv", "XNW_RESOURCE_QC": "status.json"},
                                       defaults=PROC_DEFAULTS)
    files = collect_files(tmp_path, contract, derived_root="raw")
    with caplog.at_level(logging.WARNING):
        views = collect_views(tmp_path, contract, files[DERIVED_ROLE], derived_root="raw")
    assert views == {"METRICS": [], "QC": []}                           # status.json is PROVENANCE, not on DERIVED
    assert "view METRICS matched no file on DERIVED" in caplog.text


def test_a_file_that_is_neither_the_tools_nor_the_wrapups_is_left_off_the_record_with_a_warning(tmp_path, caplog):
    """The dataset is not a place for stray files, and the fixed resources hold only the wrapup's own."""
    _tool_tree(tmp_path)
    (tmp_path / "scratch.bin").write_text("x")
    contract = RecordContract.from_env({"XNW_CARD_ID": "c"}, defaults=PROC_DEFAULTS)
    with caplog.at_level(logging.WARNING):
        files = collect_files(tmp_path, contract, derived_root="raw")
    assert "scratch.bin" not in str(_names(files))
    assert "1 file(s) in the output are neither the tool's output (raw/) nor a wrapup artefact and are not on the record: scratch.bin" in caplog.text


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
    calls = [c for c in handler.calls if c["path"] != "/data/JSESSION"]   # the lazy login is not a working request
    paths = [c["path"] for c in calls]
    # create-only: existence is checked first, then the create PUT
    assert paths[0] == "/data/experiments/XNAT_E00018/assessors/DeepWMH_scan2_X?format=json" and calls[0]["method"] == "GET"
    assert paths[1] == "/data/experiments/XNAT_E00018/assessors/DeepWMH_scan2_X?inbody=true"
    assert calls[1]["content_type"] == "application/xml" and calls[1]["body"] == b"<xml/>"
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/DERIVED/files/volumes.json?inbody=true&format=JSON" in paths
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/REPORT/files/report.html?inbody=true&format=HTML" in paths
    assert not [p for p in paths if "/resources/METRICS/" in p]                 # METRICS is a view, not a resource (0.6.0)
    # the data output rides along under DERIVED, nested paths kept, NIfTI declared as NIFTI
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/DERIVED/files/segmentation.nii.gz?inbody=true&format=NIFTI" in paths
    assert "/data/experiments/XNAT_E00018/assessors/XNAT_E99999/out/resources/DERIVED/files/sub/part.tsv?inbody=true&format=TSV" in paths
    assert all(c["cookie"] == "JSESSIONID=FAKESESSION1234" for c in calls)     # every working request on the one session
    assert outcome["uploaded"] == {"REPORT": ["report.html"],
                                   "DERIVED": ["segmentation.nii.gz", "sub/part.tsv", "volumes.json"]}
    assert outcome["skipped_empty"] == []
    assert outcome["output_paths"] == {"uploaded": ["report.html", "segmentation.nii.gz", "sub/part.tsv", "volumes.json"], "skipped_empty": []}


def test_publish_names_derived_files_relative_to_the_tree_root_not_the_output_dir(xnat, tmp_path):
    """The dataset sits at the DERIVED root: raw/sub-H025.html on disk is sub-H025.html on the record."""
    host, handler = xnat
    _tool_tree(tmp_path)
    files = collect_files(tmp_path, RecordContract.from_env({"XNW_CARD_ID": "c"}, defaults=PROC_DEFAULTS), derived_root="raw")
    outcome = publish_record(_context(host), "fmriprep_X", "<xml/>", files, output_dir=tmp_path)
    paths = [c["path"].split("/out/resources/")[1].split("?")[0] for c in handler.calls if "/out/resources/" in c["path"]]
    assert "DERIVED/files/sub-H025.html" in paths and "DERIVED/files/sub-H025/figures/a.svg" in paths
    assert not [p for p in paths if "raw/" in p]
    assert "raw/sub-H025.html" in outcome["output_paths"]["uploaded"]       # the local path, for the pointer reduction


def test_publish_skips_empty_files_instead_of_losing_the_record(xnat, tmp_path, caplog):
    """demo02 2026-09-07: a tool that wrote nothing to stderr produced a 0-byte logs/stderr.log; XNAT
    answered the in-body PUT with HTTP 500 and the whole record was rolled back."""
    host, handler = xnat
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "stdout.log").write_text("ran")
    (tmp_path / "logs" / "stderr.log").write_bytes(b"")
    files = {"LOGS": [tmp_path / "logs" / "stdout.log", tmp_path / "logs" / "stderr.log"]}
    outcome = publish_record(_context(host), "fake_X", "<xml/>", files, output_dir=tmp_path)
    assert outcome["id"] == "XNAT_E99999" and outcome["uploaded"] == {"LOGS": ["logs/stdout.log"]}
    assert outcome["skipped_empty"] == ["logs/stderr.log"]
    assert not [c for c in handler.calls if c["method"] == "DELETE"], "no rollback"
    assert not [c for c in handler.calls if "stderr.log" in c["path"]], "the empty file is never sent"
    assert "logs/stderr.log is empty; not uploaded to LOGS" in caplog.text


def test_publish_if_possible_leaves_empty_files_out_of_the_record_document(xnat, tmp_path, monkeypatch, caplog):
    """Codex P2 on PR #10: output_file_count and results_json must not claim a file the upload skips."""
    from types import SimpleNamespace
    from segwrapup.publish import publish_if_possible
    host, handler = xnat
    (tmp_path / "volumes.json").write_text('{"a":1}')
    (tmp_path / "empty.txt").write_bytes(b"")
    for k, v in {"XNW_CARD_ID": "c", "XNW_ANALYSIS_TYPE": "t", "XNW_CONTAINER_IMAGE": "i:1"}.items():
        monkeypatch.setenv(k, v)
    args = SimpleNamespace(no_publish=False, record_label="lbl_X", model="m", scan="3")
    with caplog.at_level(logging.WARNING):
        outcome = publish_if_possible(args, tmp_path, {"model": "m", "model_version": "1"}, [], False, context=_context(host))
    assert outcome["skipped_empty"] == ["empty.txt"] and outcome["uploaded"] == {"DERIVED": ["volumes.json"]}
    assert outcome["output_paths"] == {"uploaded": ["volumes.json"], "skipped_empty": ["empty.txt"]}
    xml = [c for c in handler.calls if c["method"] == "PUT" and "/out/" not in c["path"]][0]["body"].decode()
    assert "<analysis:output_file_count>1</analysis:output_file_count>" in xml and "empty.txt" not in xml
    # the record's own field carries the views, so a consumer resolves METRICS without wrapup.json
    results = json.loads(html_unescape(xml.split("<analysis:results_json>")[1].split("</analysis:results_json>")[0]))
    # per-role counts, not file lists: the lists overran the 65,536-character cap on big trees (0.6.2)
    assert results["views"] == {"METRICS": ["volumes.json"]} and results["file_counts"] == {"DERIVED": 1}
    assert outcome["views"] == {"METRICS": ["volumes.json"]}
    assert "empty.txt is empty; left off the record" in caplog.text


def test_publish_refuses_a_label_that_already_exists(xnat, tmp_path):
    """Codex P1 on PR #3: a reused --record-label would turn the create PUT into an update."""
    host, handler = xnat
    handler.existing_labels = {"DeepWMH_scan2_X"}
    with pytest.raises(RuntimeError, match="already exists"):
        publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", {})
    assert [c["method"] for c in handler.calls if c["path"] != "/data/JSESSION"] == ["GET"], "nothing is written when the label exists"


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_publish_refuses_when_the_existence_check_is_inconclusive(xnat, tmp_path, status):
    """Codex round 4 on PR #3: anything but 404 must not fall through to a create-or-update PUT."""
    host, handler = xnat
    handler.get_status = status
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        publish_record(_context(host), "DeepWMH_scan2_X", "<xml/>", {})
    assert [c["method"] for c in handler.calls if c["path"] != "/data/JSESSION"] == ["GET"]


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


def test_truncated_error_body_on_create_is_still_a_runtime_error(xnat, tmp_path, monkeypatch):
    """Codex round 7 on PR #3: error.read() raising IncompleteRead inside the HTTPError handler escaped."""
    import http.client, io, urllib.error
    import segwrapup.publish as publish
    host, handler = xnat

    class _Truncated(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b"partial")

    def urlopen_500_truncated(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, _Truncated())

    monkeypatch.setattr(publish, "_request", lambda *a, **k: 404)
    monkeypatch.setattr(publish.urllib.request, "urlopen", urlopen_500_truncated)
    with pytest.raises(RuntimeError, match=r"HTTP 500 \(error body unreadable: IncompleteRead"):
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
    contract = RecordContract.from_env({"XNW_CARD_ID": "c", "XNW_RESOURCE_metrics": "*.json", "XNW_RESOURCE_logs": "*.log"})
    assert contract.resources["METRICS"] == ["*.json"]
    assert contract.ignored_overrides == ("LOGS=*.log",)         # upper-cased before the fixed-role check


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

def _run_with_masks(tmp_path, monkeypatch, env, *extra, tool_files=None):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    write_ct_series(inp / ".source_dicom")
    write_mask(inp / "segmentation.nii.gz", blob_mask(), affine=series_ras_affine())
    for name, text in (tool_files or {}).items():           # anything else the tool wrote beside the masks
        (inp / name).parent.mkdir(parents=True, exist_ok=True)
        (inp / name).write_text(text)
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
    assert record["id"] == "XNAT_E99999" and record["label"].startswith("DeepWMH_SESS01_scan2_")   # label carries the session label
    assert "volumes.json" in record["uploaded"]["DERIVED"] and "report.html" in record["uploaded"]["REPORT"]
    assert "wrapup.json" in record["uploaded"]["PROVENANCE"] and "METRICS" not in record["uploaded"]
    assert "segmentation.nii.gz" in record["uploaded"]["DERIVED"] and "segmentation.tsv" in record["uploaded"]["DERIVED"]
    # the METRICS view names the measurement files inside DERIVED, in wrapup.json (the copy on
    # PROVENANCE included, since it is uploaded after the views are known) and on the record
    assert record["views"] == manifest["views"] == {"METRICS": ["volumes.json", "volumes.csv", "segmentation.tsv"]}
    uploaded_manifest = next(c for c in handler.calls if c["method"] == "PUT" and c["path"].split("?")[0].endswith("/PROVENANCE/files/wrapup.json"))
    assert json.loads(uploaded_manifest["body"])["views"] == {"METRICS": ["volumes.json", "volumes.csv", "segmentation.tsv"]}
    create = next(c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"])
    assert create["path"].endswith(f"/assessors/{record['label']}?inbody=true")
    assert b"<analysis:pipeline_name>DeepWMH</analysis:pipeline_name>" in create["body"]
    assert b"<analysis:card_id>deepwmh</analysis:card_id>" in create["body"]


def test_cli_keeps_the_tools_dotfiles_under_raw_and_never_uploads_the_dicom_copy(xnat, tmp_path, monkeypatch):
    """seg-wrapup path: the tool's tree keeps its raw/ prefix on a segmentation record and since 0.6.1
    its dotfiles travel with it; the parent's .source_dicom is consumed by the DICOM SEG step and
    never copied to the output or uploaded (README, D10: "minus the DICOM copy")."""
    host, handler = xnat
    env = {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CONTRACT": json.dumps(CONTRACT)}
    code, manifest, out = _run_with_masks(tmp_path, monkeypatch, env,
                                          tool_files={".bidsignore": "*.html\n", "stats/.cache": "k", "stats/statistics.json": "{}"})
    assert code == 0
    uploaded = manifest["analysis_record"]["uploaded"]
    assert {"raw/.bidsignore", "raw/stats/.cache", "raw/stats/statistics.json", "segmentation.nii.gz"} <= set(uploaded["DERIVED"])
    assert not [name for names in uploaded.values() for name in names if ".source_dicom" in name]
    assert not [c["path"] for c in handler.calls if ".source_dicom" in c["path"]]
    assert not (out / ".source_dicom").exists() and not (out / "raw" / ".source_dicom").exists()
    assert (out / "raw" / ".bidsignore").read_text() == "*.html\n"


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


# ── one XNAT session per run ───────────────────────────────────────────────────

def test_cli_opens_one_session_uses_it_everywhere_and_closes_it(xnat, tmp_path, monkeypatch):
    """James, 2026-09-06: '80 sessions open from 3 IPs'. A run must log in once, send the
    cookie on every request, and log out at the end, instead of one server session per request."""
    host, handler = xnat
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    write_ct_series(inp / ".source_dicom")
    write_mask(inp / "segmentation.nii.gz", blob_mask(), affine=series_ras_affine())
    for key, value in {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CONTRACT": json.dumps(CONTRACT)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SEG_NO_REGISTER", raising=False)
    assert cli.main(["--input", str(inp), "--output", str(out), "--model", "DeepWMH", "--model-version", "1.0.1"]) == 0
    logins = [c for c in handler.calls if c["method"] == "POST" and c["path"] == "/data/JSESSION"]
    logouts = [c for c in handler.calls if c["method"] == "DELETE" and c["path"] == "/data/JSESSION"]
    assert len(logins) == 1 and logins[0]["auth"].startswith("Basic ")
    assert len(logouts) == 1 and handler.calls[-1] is logouts[0], "logout is the last request"
    working = [c for c in handler.calls if c["path"] != "/data/JSESSION"]
    assert len(working) >= 10, "ROI registration, label probe, create and uploads"
    assert all(c["cookie"] == "JSESSIONID=FAKESESSION1234" and c["auth"] is None for c in working), \
        "every working request rides the one session, none carries Basic auth"
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["analysis_record"]["id"] == "XNAT_E99999" and manifest["roi_collection"]["status"] == 200


def test_cli_closes_the_session_even_when_publishing_fails(xnat, tmp_path, monkeypatch):
    host, handler = xnat
    handler.fail_paths = {"/data/experiments/XNAT_E00018/assessors/"}
    env = {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CONTRACT": json.dumps(CONTRACT)}
    code, manifest, out = _run_with_masks(tmp_path, monkeypatch, env)
    assert code == 0 and "error" in manifest["analysis_record"]
    assert handler.calls[-1]["method"] == "DELETE" and handler.calls[-1]["path"] == "/data/JSESSION"


def test_cli_falls_back_to_basic_auth_when_login_fails(xnat, tmp_path, monkeypatch, caplog):
    host, handler = xnat
    handler.fail_paths = {"/data/JSESSION"}
    env = {**CONTEXT_ENV, "XNAT_HOST": host, "XNW_CONTRACT": json.dumps(CONTRACT)}
    with caplog.at_level(logging.WARNING):
        code, manifest, out = _run_with_masks(tmp_path, monkeypatch, env)
    assert code == 0 and manifest["analysis_record"]["id"] == "XNAT_E99999"
    assert "falling back to Basic auth" in caplog.text
    working = [c for c in handler.calls if c["path"] != "/data/JSESSION"]
    assert all((c["auth"] or "").startswith("Basic ") and c["cookie"] is None for c in working)
    assert not [c for c in handler.calls if c["method"] == "DELETE" and c["path"] == "/data/JSESSION"], "nothing to log out of"


def test_cli_without_xnat_context_never_touches_the_session_endpoint(xnat, tmp_path, monkeypatch):
    host, handler = xnat
    code, manifest, out = _run_with_masks(tmp_path, monkeypatch, {})
    assert code == 0 and not [c for c in handler.calls if c["path"] == "/data/JSESSION"]


def test_create_409_retries_once_with_a_suffix_and_relabels_the_document(xnat, tmp_path, caplog):
    from segwrapup import publish
    """Merlin batch 2026-09-06: labels are unique per project, the per-session probe cannot see a
    label taken on another session, and two wrapups finishing in the same second collided."""
    host, handler = xnat
    handler.conflict_labels = {"merlin_scan2_X_record"}
    out = tmp_path / "out"; out.mkdir(); (out / "report.html").write_text("<p>r</p>")
    context = XnatContext(host=host, user="u", password="p", project="P", session="XNAT_E00018", scan="2")
    xml = '<analysis:SessionAnalysis xmlns:analysis="x" project="P" label="merlin_scan2_X_record">\n</analysis:SessionAnalysis>'
    with caplog.at_level(logging.WARNING):
        result = publish.publish_record(context, "merlin_scan2_X_record", xml, {"REPORT": [out / "report.html"]}, output_dir=out)
    creates = [c for c in handler.calls if c["method"] == "PUT" and "/assessors/" in c["path"] and "/out/" not in c["path"]]
    assert len(creates) == 2 and creates[0]["path"].startswith("/data/experiments/XNAT_E00018/assessors/merlin_scan2_X_record?")
    retry_label = creates[1]["path"].split("/assessors/")[1].split("?")[0]
    assert retry_label.startswith("merlin_scan2_X_record_") and len(retry_label) == len("merlin_scan2_X_record_") + 4
    assert f'label="{retry_label}"' in creates[1]["body"].decode() and 'label="merlin_scan2_X_record"' not in creates[1]["body"].decode()
    assert result["label"] == retry_label and "retrying once as " + retry_label in caplog.text


def test_create_409_twice_is_reported_not_looped(xnat, tmp_path):
    from segwrapup import publish
    host, handler = xnat
    handler.conflict_labels = None   # sentinel: every create conflicts
    out = tmp_path / "out"; out.mkdir(); (out / "report.html").write_text("<p>r</p>")
    context = XnatContext(host=host, user="u", password="p", project="P", session="XNAT_E00018", scan="2")
    xml = '<analysis:SessionAnalysis xmlns:analysis="x" project="P" label="L">\n</analysis:SessionAnalysis>'
    with pytest.raises(RuntimeError, match="HTTP 409"):
        publish.publish_record(context, "L", xml, {"REPORT": [out / "report.html"]}, output_dir=out)
    assert len([c for c in handler.calls if c["method"] == "PUT"]) == 2


# ── subject scope (0.6.2) ───────────────────────────────────────────────────────

def _subject_context(host):
    return XnatContext(host=host, user="alias", password="secret", project="PROJ_1", session="", subject="XNAT_S09007")


def test_record_xml_at_subject_scope_is_a_subject_assessor_without_scans(tmp_path):
    from segwrapup.publish import SUBJECT_XSI_TYPE, xsi_type_for
    (tmp_path / "volumes.json").write_text("{}")
    contract = RecordContract.from_env({"XNW_CONTRACT": json.dumps(CONTRACT)})
    context = _subject_context("http://x")
    assert context.scope == "subject" and context.target == "XNAT_S09007" and xsi_type_for(context) == SUBJECT_XSI_TYPE
    xml = build_record_xml(context, contract, "fmriprep_292_X", _report(), [], collect_files(tmp_path, contract), False)
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<analysis:SubjectAnalysis ')
    assert 'project="PROJ_1" label="fmriprep_292_X"' in xml
    assert "<xnat:subject_ID>XNAT_S09007</xnat:subject_ID>" in xml and "imageSession_ID" not in xml
    assert "<analysis:scans>" not in xml and xml.rstrip().endswith("</analysis:SubjectAnalysis>")
    assert "<analysis:pipeline_name>DeepWMH<" in xml       # the same fields as a session record


def test_publish_at_subject_scope_creates_under_the_subject_and_uploads_to_experiment_resources(xnat, tmp_path):
    from segwrapup.publish import SUBJECT_XSI_TYPE
    host, handler = xnat
    (tmp_path / "report.html").write_text("<html/>")
    (tmp_path / "sub-292").mkdir()
    (tmp_path / "sub-292" / "ses-preop_T1w.json").write_text("{}")
    files = collect_files(tmp_path, RecordContract())
    outcome = publish_record(_subject_context(host), "fmriprep_292_X", "<xml/>", files, output_dir=tmp_path)
    assert outcome["id"] == "XNAT_E99999" and outcome["xsi_type"] == SUBJECT_XSI_TYPE
    calls = [c for c in handler.calls if c["path"] != "/data/JSESSION"]
    paths = [c["path"] for c in calls]
    assert paths[0] == "/data/projects/PROJ_1/subjects/XNAT_S09007/experiments/fmriprep_292_X?format=json" and calls[0]["method"] == "GET"
    assert paths[1] == "/data/projects/PROJ_1/subjects/XNAT_S09007/experiments/fmriprep_292_X?inbody=true"
    assert "/data/experiments/XNAT_E99999/resources/REPORT/files/report.html?inbody=true&format=HTML" in paths
    assert "/data/experiments/XNAT_E99999/resources/DERIVED/files/sub-292/ses-preop_T1w.json?inbody=true&format=JSON" in paths
    assert not [p for p in paths if "/out/" in p or "/assessors/" in p]


def test_publish_at_subject_scope_refuses_an_existing_label_and_rolls_back_by_experiment_id(xnat, tmp_path):
    host, handler = xnat
    handler.existing_labels = {"taken_X"}
    (tmp_path / "a.txt").write_text("a")
    files = collect_files(tmp_path, RecordContract())
    with pytest.raises(RuntimeError, match="already exists on XNAT_S09007"):
        publish_record(_subject_context(host), "taken_X", "<xml/>", files, output_dir=tmp_path)
    handler.existing_labels = set()
    handler.fail_paths = {"/data/experiments/XNAT_E99999/resources/DERIVED/files/a.txt"}
    with pytest.raises(RuntimeError, match="record XNAT_E99999 deleted"):
        publish_record(_subject_context(host), "fresh_X", "<xml/>", files, output_dir=tmp_path)
    deletes = [c["path"] for c in handler.calls if c["method"] == "DELETE" and c["path"] != "/data/JSESSION"]
    assert deletes == ["/data/experiments/XNAT_E99999?removeFiles=true"]


def test_results_json_stays_under_the_schema_cap_on_a_big_tree(tmp_path, caplog):
    """fmriprep's full run (1,036 files) and hippunfold were refused with results_json at
    79,940 characters against the 65,536 cap; the file lists are counts now and an
    overrunning view list is reduced to counts with a note."""
    from segwrapup.publish import RESULTS_JSON_MAX, bounded_results_json
    contract = RecordContract.from_env({"XNW_CONTRACT": json.dumps(CONTRACT)})
    files = {"DERIVED": [tmp_path / f"sub-01/func/sub-01_task-rest_run-{i:04d}_desc-preproc_bold.nii.gz" for i in range(3000)]}
    views = {"METRICS": [f"sub-01/func/sub-01_task-rest_run-{i:04d}_desc-confounds_timeseries.tsv" for i in range(3000)]}
    for f in files["DERIVED"]:
        f.parent.mkdir(parents=True, exist_ok=True); f.write_bytes(b"x")
    with caplog.at_level(logging.WARNING):
        xml = build_record_xml(_context("http://x"), contract, "big_X", _report(), [], files, False, output_dir=tmp_path, views=views)
    text = xml.split("<analysis:results_json>")[1].split("</analysis:results_json>")[0]
    assert len(text) <= RESULTS_JSON_MAX
    summary = json.loads(html_unescape(text))
    assert summary["file_counts"] == {"DERIVED": 3000} and summary["views"] == {"METRICS": 3000} and summary["truncated"] == ["views"]
    assert "replaced by counts" in caplog.text
    # a small tree keeps its view paths verbatim
    small = json.loads(bounded_results_json({"views": {"METRICS": ["a.json"]}, "file_counts": {"DERIVED": 1}}))
    assert small["views"] == {"METRICS": ["a.json"]} and "truncated" not in small
