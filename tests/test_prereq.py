"""record-fetch: resolve a card's prerequisites by REST and materialise them next to the input.

A fake XNAT answers the project record listing, the record/resource file listings and the file
downloads; every request is recorded.
"""
import html
import json
import logging
from html import unescape as html_unescape
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from segwrapup import prereq
from segwrapup.prereq import Prerequisite, choose_record, prerequisites_from_env
from segwrapup.register import XnatContext

T = "analysis:sessionanalysisdata/"
def rec(id_, label, pipeline, version, review, run, insert, session="XNAT_E1", atype="diffusion-preprocessing"):
    return {"ID": id_, "label": label, "insert_date": insert, T + "imagesession_id": session, T + "pipeline_name": pipeline,
            T + "pipeline_version": version, T + "analysis_type": atype, T + "review_state": review, T + "run_status": run,
            T + "publication_status": "DRAFT"}

RECORDS = [
    rec("XNAT_E10", "qsiprep_S1_20260907T100000Z_record", "qsiprep", "1.1.1", "PENDING_REVIEW", "SUCCEEDED", "2026-09-07 10:00:00"),
    rec("XNAT_E11", "qsiprep_S1_20260907T110000Z_record", "qsiprep", "1.1.1", "ACCEPTED", "SUCCEEDED", "2026-09-07 11:00:00"),
    rec("XNAT_E12", "qsiprep_S1_20260907T120000Z_record", "qsiprep", "1.1.1", "PENDING_REVIEW", "SUCCEEDED", "2026-09-07 12:00:00"),
    rec("XNAT_E13", "qsiprep_S1_20260907T130000Z_record", "qsiprep", "1.1.1", "PENDING_REVIEW", "FAILED", "2026-09-07 13:00:00"),
    rec("XNAT_E14", "qsiprep_old_record", "qsiprep", "0.20.0", "ACCEPTED", "SUCCEEDED", "2026-09-06 09:00:00"),
    rec("XNAT_E20", "mriqc_S1_record", "mriqc", "24.0.2", "ACCEPTED", "SUCCEEDED", "2026-09-07 09:00:00", atype="qc"),
    rec("XNAT_E99", "qsiprep_other_session", "qsiprep", "1.1.1", "ACCEPTED", "SUCCEEDED", "2026-09-07 14:00:00", session="XNAT_E2"),
]
FILES = {"XNAT_E12": {"DERIVED": ["sub-1/dwi/preproc.nii.gz", "sub-1/qc.json"], "PROVENANCE": ["wrapup.json"]},      # published by 0.6.0
         "XNAT_E11": {"DERIVED": ["sub-1/dwi/preproc.nii.gz"], "METRICS": ["sub-1/qc.json"], "PROVENANCE": ["wrapup.json"]}}   # by 0.5.0
# wrapup.json as served from PROVENANCE: a 0.6.0 record maps its view roles onto DERIVED paths; a 0.5.0 one has no views
WRAPUP_JSON = {"XNAT_E12": {"wrapup": "proc-wrapup", "version": "0.6.0", "views": {"METRICS": ["sub-1/qc.json"]}},
               "XNAT_E11": {"wrapup": "proc-wrapup", "version": "0.5.0"}}
SESSION_RESOURCES = {"BIDS": ["sub-1/anat/sub-1_T1w.nii.gz", "dataset_description.json"]}
TS = "analysis:subjectanalysisdata/"
def subrec(id_, label, pipeline, version, review, run, insert, subject="XNAT_S1", atype="functional-preprocessing"):
    return {"ID": id_, "label": label, "insert_date": insert, TS + "subject_id": subject, TS + "pipeline_name": pipeline,
            TS + "pipeline_version": version, TS + "analysis_type": atype, TS + "review_state": review, TS + "run_status": run,
            TS + "publication_status": "DRAFT"}
SUBJECT_RECORDS = [subrec("XNAT_E50", "fmriprep_292_record", "fmriprep", "25.2.5", "ACCEPTED", "SUCCEEDED", "2026-09-10 03:00:00"),
                   subrec("XNAT_E51", "fmriprep_other_subject", "fmriprep", "25.2.5", "ACCEPTED", "SUCCEEDED", "2026-09-10 04:00:00", subject="XNAT_S2")]
SUBJECT_SESSIONS = [{"ID": "XNAT_E1", "label": "S1", "xsiType": "xnat:mrSessionData"}, {"ID": "XNAT_E2", "label": "S2", "xsiType": "xnat:mrSessionData"},
                    {"ID": "XNAT_E77", "label": "S1_record", "xsiType": "analysis:subjectAnalysisData"}]
FILES["XNAT_E50"] = {"DERIVED": ["sub-292/ses-preop/func/bold.nii.gz", "sub-292/ses-postop/func/bold.nii.gz"], "PROVENANCE": ["wrapup.json"]}
FILES["XNAT_E99"] = {"DERIVED": ["sub-1/dwi/preproc.nii.gz"], "PROVENANCE": ["wrapup.json"]}
SCANS = {"2": {"type": "T1w", "DICOM": ["1.dcm", "2.dcm"]}, "3": {"type": "BOLD", "DICOM": ["b1.dcm"]}, "4": {"type": "T1w", "DICOM": []}}


class _Xnat(BaseHTTPRequestHandler):
    calls: list = []

    def _json(self, obj):
        body = json.dumps(obj).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a): pass

    def do_POST(self):
        _Xnat.calls.append(("POST", self.path)); self.send_response(200); self.end_headers(); self.wfile.write(b"FAKESESSION")

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", "0")); body = self.rfile.read(n)
        _Xnat.calls.append(("PUT", self.path, self.headers.get("Cookie"), body))
        create = ("/assessors/" in self.path and "/out/" not in self.path) or ("/subjects/" in self.path and "/experiments/" in self.path and "/resources/" not in self.path)
        self.send_response(201 if create else 200); self.end_headers(); self.wfile.write(b"XNAT_E77" if create else b"")

    def do_DELETE(self):
        _Xnat.calls.append(("DELETE", self.path)); self.send_response(200); self.end_headers()

    def do_GET(self):
        _Xnat.calls.append(("GET", self.path, self.headers.get("Cookie")))
        p = self.path
        if p.startswith("/data/projects/P/experiments?"):
            raise AssertionError("the project-wide listing must not be used; records are listed per owner: " + p)
        if p.startswith(("/data/experiments/XNAT_E1/assessors?xsiType=analysis:sessionAnalysisData", "/data/experiments/XNAT_E2/assessors?xsiType=analysis:sessionAnalysisData")):
            owner = p.split("/data/experiments/")[1].split("/")[0]
            return self._json({"ResultSet": {"Result": [r for r in RECORDS if r[T + "imagesession_id"] == owner]}})
        if p.startswith("/data/projects/P/subjects/XNAT_S1/experiments?xsiType=analysis:subjectAnalysisData"):
            assert "analysis:subjectAnalysisData/subject_ID" in p, "the subject record listing must ask for the owner column"
            return self._json({"ResultSet": {"Result": [r for r in SUBJECT_RECORDS if r[TS + "subject_id"] == "XNAT_S1"]}})
        if p == "/data/experiments/XNAT_E1?format=json":                      # session label (and subject) for the record label
            return self._json({"items": [{"data_fields": {"label": "S1", "subject_ID": "XNAT_S1"}}]})
        if p == "/data/projects/P/subjects/XNAT_S1?format=json":
            return self._json({"items": [{"data_fields": {"label": "292"}}]})
        if p.startswith("/data/projects/P/subjects/XNAT_S1/experiments?format=json"):
            return self._json({"ResultSet": {"Result": SUBJECT_SESSIONS}})
        if "/subjects/" in p and "/experiments/" in p and p.endswith("?format=json"):   # subject-scope label probe: free
            self.send_response(404); self.end_headers(); return
        if p.startswith("/data/experiments/XNAT_E50/resources/") and p.endswith("/files?format=json"):
            role = p.split("/resources/")[1].split("/")[0]
            names = FILES["XNAT_E50"].get(role, [])
            return self._json({"ResultSet": {"Result": [{"Name": n.rsplit("/", 1)[-1], "URI": f"/data/experiments/XNAT_E50/resources/{role}/files/{n}", "Size": 3} for n in names]}})
        if "/assessors/" in p and "/out/" not in p:                          # label probe: free
            self.send_response(404); self.end_headers(); return
        if "/out/resources/" in p and p.endswith("/files?format=json"):
            assert p.startswith(("/data/experiments/XNAT_E1/assessors/", "/data/experiments/XNAT_E2/assessors/")), "record files are listed assessor-scoped (experiment-scoped answers the document)"
            owner = p.split("/data/experiments/")[1].split("/")[0]
            rid = p.split("/assessors/")[1].split("/")[0]; role = p.split("/out/resources/")[1].split("/")[0]
            assert (rid == "XNAT_E99") == (owner == "XNAT_E2"), f"record {rid} is not an assessor of {owner}"
            names = FILES.get(rid, {}).get(role, [])
            return self._json({"ResultSet": {"Result": [{"Name": n.rsplit("/", 1)[-1], "URI": f"/data/experiments/{owner}/assessors/{rid}/out/resources/{role}/files/{n}", "Size": 3} for n in names]}})
        if p == "/data/experiments/XNAT_E1/scans?format=json":
            return self._json({"ResultSet": {"Result": [{"ID": k, "type": v["type"], "series_description": v["type"]} for k, v in SCANS.items()]}})
        if "/data/experiments/XNAT_E1/scans/" in p and p.endswith("/files?format=json"):
            scan = p.split("/scans/")[1].split("/")[0]; label = p.split("/resources/")[1].split("/")[0]
            names = SCANS.get(scan, {}).get(label, [])
            return self._json({"ResultSet": {"Result": [{"Name": n, "URI": f"/data/experiments/XNAT_E1/scans/{scan}/resources/{label}/files/{n}", "Size": 3} for n in names]}})
        if ("/data/experiments/XNAT_E1/resources/" in p or "/data/experiments/XNAT_E2/resources/" in p) and p.endswith("/files?format=json"):
            owner = p.split("/data/experiments/")[1].split("/")[0]
            label = p.split("/resources/")[1].split("/")[0]
            if label not in SESSION_RESOURCES:                                   # XNAT: 404 for a resource that does not exist
                self.send_response(404); self.end_headers(); return
            names = SESSION_RESOURCES.get(label, [])
            return self._json({"ResultSet": {"Result": [{"Name": n.rsplit("/", 1)[-1], "URI": f"/data/experiments/{owner}/resources/{label}/files/{n}", "Size": 3} for n in names]}})
        if p.endswith("/PROVENANCE/files/wrapup.json") and ("/assessors/" in p or p.startswith("/data/experiments/XNAT_E50/")):
            rid = p.split("/assessors/")[1].split("/")[0] if "/assessors/" in p else "XNAT_E50"
            if rid in WRAPUP_JSON:
                return self._json(WRAPUP_JSON[rid])
            self.send_response(404); self.end_headers(); return
        if "/files/" in p:
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream"); self.end_headers(); self.wfile.write(b"data:" + p.rsplit("/", 1)[-1].encode()); return
        self.send_response(404); self.end_headers()


@pytest.fixture
def xnat(monkeypatch):
    _Xnat.calls = []
    server = HTTPServer(("127.0.0.1", 0), _Xnat); threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{server.server_port}"
    for k in list(prereq.os.environ):
        if k.startswith("XNW_PREREQ_"): monkeypatch.delenv(k)
    for k, v in {"XNAT_HOST": host, "XNAT_USER": "u", "XNAT_PASS": "p", "PROC_PROJECT": "P", "PROC_SESSION_ID": "XNAT_E1"}.items():
        monkeypatch.setenv(k, v)
    yield host, _Xnat
    server.shutdown()


# ── rules ─────────────────────────────────────────────────────────────────────

def flat(records):
    return sorted([{**{"ID": r["ID"], "label": r["label"], "insert_date": r["insert_date"]}, **{k[len(T):]: v for k, v in r.items() if k.startswith(T)}}
                   for r in records if r[T + "imagesession_id"] == "XNAT_E1"], key=lambda r: r["insert_date"], reverse=True)


def test_parse_specs_and_reject_empty():
    p = Prerequisite.parse("QSIPREP", "type=diffusion-preprocessing;pipeline=qsiprep;min=1.1;role=DERIVED;accepted=true")
    assert (p.name, p.analysis_type, p.pipeline, p.min_version, p.role, p.accepted) == ("qsiprep", "diffusion-preprocessing", "qsiprep", "1.1", "DERIVED", True)
    assert Prerequisite.parse("BIDS", "resource=BIDS").is_resource
    with pytest.raises(ValueError):
        Prerequisite.parse("X", "role=DERIVED")
    with pytest.raises(ValueError, match="unknown clause 'pipline'"):                       # Codex P1: a misspelt key must not widen the match
        Prerequisite.parse("X", "type=qc;pipline=mriqc")
    with pytest.raises(ValueError, match="is not key=value"):
        Prerequisite.parse("X", "type=qc;accepted")
    with pytest.raises(ValueError, match="accepted= must be true or false, not 'ture'"):   # Codex P1: a typo must not disable the gate
        Prerequisite.parse("X", "pipeline=qsiprep;accepted=ture")
    assert Prerequisite.parse("X", "pipeline=qsiprep;accepted=0").accepted is False
    assert [p.name for p in prerequisites_from_env({"XNW_PREREQ_B": "resource=BIDS", "XNW_PREREQ_A": "pipeline=x", "OTHER": "1"})] == ["a", "b"]


def test_repeated_clause_is_refused():
    """Codex P1 (PR #10): the last value won, so accepted=true;accepted=false disabled the gate."""
    with pytest.raises(ValueError, match="clause 'pipeline' given twice"):
        Prerequisite.parse("X", "pipeline=trusted;pipeline=other")
    with pytest.raises(ValueError, match="clause 'accepted' given twice"):
        Prerequisite.parse("X", "pipeline=qsiprep;accepted=true;accepted=false")


def test_scan_type_needs_scan_scope():
    """Codex P1 (PR #10): resource=DICOM;scan_type=T1* took the session-resource branch and dropped
    the filter, handing the run the session resource instead of the matching scans."""
    with pytest.raises(ValueError, match="scan_type= needs scope=scan"):
        Prerequisite.parse("X", "resource=DICOM;scan_type=T1*")
    with pytest.raises(ValueError, match="scan_type= needs scope=scan"):
        Prerequisite.parse("X", "resource=DICOM;scope=session;scan_type=T1*")
    assert Prerequisite.parse("X", "resource=DICOM;scope=scan;scan_type=T1*").scan_type == "T1*"


def test_prerequisite_is_a_resource_or_a_record_never_both():
    """Codex P2 (PR #10): resource=BIDS;pipeline=qsiprep;accepted=true took the resource branch and
    dropped the pipeline and review requirements without a word."""
    with pytest.raises(ValueError, match="resource= cannot be combined with pipeline=, accepted="):
        Prerequisite.parse("X", "resource=BIDS;pipeline=qsiprep;accepted=true")
    with pytest.raises(ValueError, match="resource= cannot be combined with type=, min=, role=, id="):
        Prerequisite.parse("X", "resource=BIDS;type=qc;min=1;role=DERIVED;id=XNAT_E1")
    # 0.6.2: a record prerequisite carries scope=session (default) or scope=subject; scan_type stays a resource clause
    assert Prerequisite.parse("X", "pipeline=qsiprep;scope=session").scope == "session"
    assert Prerequisite.parse("X", "pipeline=fmriprep;scope=subject").scope == "subject"
    with pytest.raises(ValueError, match="scan_type= applies only to resource="):
        Prerequisite.parse("X", "pipeline=qsiprep;scan_type=T1*")
    assert Prerequisite.parse("X", "resource=DICOM;scope=scan;scan_type=T1*").scan_type == "T1*"
    with pytest.raises(ValueError, match="resource= cannot be combined with accepted="):   # even accepted=false is a record clause
        Prerequisite.parse("X", "resource=BIDS;accepted=false")


def test_prerequisite_name_is_one_safe_path_component():
    """Codex P2 (PR #10): the name becomes prereq/<name>/, so '..' or a slash would escape the output."""
    for bad in ("..", "a/b", "a\\b", "", "1abc", "a b", "a-b", "x" * 65):
        with pytest.raises(ValueError, match="prerequisite name must match"):
            Prerequisite.parse(bad, "resource=BIDS")
    assert Prerequisite.parse("T1_DICOM", "resource=DICOM;scope=scan").name == "t1_dicom"
    with pytest.raises(ValueError, match="both name the prerequisite 'bids'"):
        prerequisites_from_env({"XNW_PREREQ_BIDS": "resource=BIDS", "XNW_PREREQ_bids": "resource=BIDS2"})


def test_choose_record_newest_succeeded_then_accepted_then_version_then_explicit():
    records = flat(RECORDS)
    newest, why = choose_record(Prerequisite.parse("q", "pipeline=qsiprep"), records)
    assert newest["ID"] == "XNAT_E12" and why == ""                       # E13 is newer but FAILED; E99 is another session
    accepted, _ = choose_record(Prerequisite.parse("q", "pipeline=qsiprep;accepted=true"), records)
    assert accepted["ID"] == "XNAT_E11"                                   # newest ACCEPTED SUCCEEDED
    versioned, _ = choose_record(Prerequisite.parse("q", "pipeline=qsiprep;accepted=true;min=1.1"), records)
    assert versioned["ID"] == "XNAT_E11"
    none, why = choose_record(Prerequisite.parse("q", "pipeline=qsiprep;min=2.0"), records)
    assert none is None and "version >= 2.0" in why
    explicit, _ = choose_record(Prerequisite.parse("q", "id=XNAT_E10"), records)
    assert explicit["ID"] == "XNAT_E10"                                   # explicit id wins even though PENDING
    missing, why = choose_record(Prerequisite.parse("q", "id=XNAT_E99"), records)
    assert missing is None and "not on this session" in why
    by_type, _ = choose_record(Prerequisite.parse("q", "type=qc"), records)
    assert by_type["ID"] == "XNAT_E20"
    nothing, why = choose_record(Prerequisite.parse("q", "pipeline=fmriprep"), records)
    assert nothing is None and why.startswith("needs a record from pipeline fmriprep (any version, review not required, run SUCCEEDED), files from its DERIVED resource; this session has no such record (its records: XNAT_E13 (qsiprep 1.1.1, FAILED, PENDING_REVIEW); XNAT_E12")
    assert why.endswith("; and 1 more)")                                     # five shown, the rest counted
    unreviewed = [r for r in records if r["ID"] not in ("XNAT_E11", "XNAT_E14")]
    gated, why = choose_record(Prerequisite.parse("q", "type=diffusion-preprocessing;pipeline=qsiprep;accepted=true;min=1.1"), unreviewed)
    assert gated is None and why == ("needs a diffusion-preprocessing record from pipeline qsiprep (version >= 1.1, ACCEPTED in review, run SUCCEEDED), "
                                     "files from its DERIVED resource; none of the 2 SUCCEEDED record(s) is ACCEPTED, review one first: "
                                     "XNAT_E12 (qsiprep 1.1.1, SUCCEEDED, PENDING_REVIEW); XNAT_E10 (qsiprep 1.1.1, SUCCEEDED, PENDING_REVIEW)")


# ── end to end ────────────────────────────────────────────────────────────────

def test_record_fetch_passes_input_through_and_materialises_record_and_resource(xnat, tmp_path, monkeypatch, caplog):
    host, handler = xnat
    inp = tmp_path / "in"; (inp / "sub-1").mkdir(parents=True); (inp / "sub-1" / "orig.txt").write_text("x")
    out = tmp_path / "out"
    monkeypatch.setenv("XNW_PREREQ_QSIPREP", "type=diffusion-preprocessing;pipeline=qsiprep;role=DERIVED")
    monkeypatch.setenv("XNW_PREREQ_BIDS", "resource=BIDS")
    with caplog.at_level(logging.INFO):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 0
    assert (out / "sub-1" / "orig.txt").read_text() == "x"                                     # pass-through
    # the record's DERIVED is the dataset at its root, so it lands at prereq/<name>/ with no raw/ segment (0.6.0)
    assert (out / "prereq" / "qsiprep" / "sub-1" / "dwi" / "preproc.nii.gz").read_bytes() == b"data:preproc.nii.gz"
    assert not (out / "prereq" / "qsiprep" / "raw").exists()
    assert (out / "prereq" / "bids" / "dataset_description.json").exists()
    m = json.loads((out / "prereq.json").read_text())
    q = next(p for p in m["prerequisites"] if p["name"] == "qsiprep")
    assert q["record"]["ID"] == "XNAT_E12" and q["role"] == "DERIVED" and q["files"] == 2 and q["path"] == "prereq/qsiprep"
    assert "prerequisite qsiprep: XNAT_E12" in caplog.text
    # one session, cookie on every call, closed at the end; records are listed per owner
    assert [c[0] for c in handler.calls if c[1] == "/data/JSESSION"] == ["POST", "DELETE"]
    assert all(c[2] == "JSESSIONID=FAKESESSION" for c in handler.calls if c[0] == "GET")
    assert any(c[1].startswith("/data/experiments/XNAT_E1/assessors?xsiType=analysis:sessionAnalysisData") for c in handler.calls), "records are listed through the session's own assessors endpoint"


def test_a_view_role_resolves_through_wrapup_json_to_the_derived_paths_it_names(xnat, tmp_path, monkeypatch, caplog):
    """0.6.0: METRICS is not a resource on the record; role=METRICS reads the record's wrapup.json
    views and copies exactly those DERIVED files, at their DERIVED paths."""
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); out = tmp_path / "out"
    monkeypatch.setenv("XNW_PREREQ_QC", "pipeline=qsiprep;role=METRICS")
    with caplog.at_level(logging.INFO):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 0
    assert (out / "prereq" / "qc" / "sub-1" / "qc.json").read_bytes() == b"data:qc.json"
    assert not (out / "prereq" / "qc" / "sub-1" / "dwi").exists()                              # only what the view names
    q = json.loads((out / "prereq.json").read_text())["prerequisites"][0]
    assert q["record"]["ID"] == "XNAT_E12" and q["role"] == "METRICS" and q["files"] == 1
    assert not [c for c in handler.calls if "/out/resources/METRICS/" in c[1]]                 # no such resource is ever asked for
    assert any(c[1].endswith("/assessors/XNAT_E12/out/resources/PROVENANCE/files/wrapup.json") for c in handler.calls)
    assert "needs a record from pipeline qsiprep (any version, review not required, run SUCCEEDED), the DERIVED files its METRICS view names" not in caplog.text


def test_a_record_published_before_views_existed_still_serves_its_role_resource(xnat, tmp_path, monkeypatch, caplog):
    """A 0.5.0 record has a real METRICS resource and no views in wrapup.json: used as it was."""
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); out = tmp_path / "out"
    monkeypatch.setenv("XNW_PREREQ_QC", "id=XNAT_E11;role=METRICS")
    with caplog.at_level(logging.INFO):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 0
    assert (out / "prereq" / "qc" / "sub-1" / "qc.json").read_bytes() == b"data:qc.json"
    assert any("/assessors/XNAT_E11/out/resources/METRICS/files?format=json" in c[1] for c in handler.calls)
    assert "record XNAT_E11 carries no views (published before 0.6.0); reading its METRICS resource" in caplog.text


def test_a_view_the_record_does_not_map_is_an_unmet_prerequisite_with_the_views_it_has(xnat, tmp_path, monkeypatch, caplog):
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); out = tmp_path / "out"
    monkeypatch.setenv("XNW_PREREQ_CONN", "id=XNAT_E12;role=CONNECTOME")
    with caplog.at_level(logging.ERROR):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 3
    assert ("prerequisite conn: needs record XNAT_E12 (the DERIVED files its CONNECTOME view names); "
            "record XNAT_E12 has no CONNECTOME view; its wrapup.json maps METRICS") in caplog.text
    assert not [c for c in handler.calls if "/DERIVED/files/" in c[1] and not c[1].endswith("format=json")]   # nothing downloaded


def test_record_fetch_fails_fast_when_a_prerequisite_is_unmet(xnat, tmp_path, monkeypatch, caplog):
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); out = tmp_path / "out"
    monkeypatch.setenv("XNW_PREREQ_QSIPREP", "pipeline=qsiprep;accepted=true;min=2.0")
    with caplog.at_level(logging.ERROR):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 3
    assert "prerequisite qsiprep: needs a record from pipeline qsiprep (version >= 2.0, ACCEPTED in review, run SUCCEEDED), files from its DERIVED resource; no SUCCEEDED record at version >= 2.0: XNAT_E13 (qsiprep 1.1.1, FAILED, PENDING_REVIEW); XNAT_E12" in caplog.text
    assert "Failed (Setup)" in caplog.text
    m = json.loads((out / "prereq.json").read_text())
    assert "no SUCCEEDED record at version >= 2.0" in m["prerequisites"][0]["error"]
    assert not (out / "prereq" / "qsiprep").exists() or not any((out / "prereq" / "qsiprep").iterdir())
    assert not [c for c in handler.calls if "/files/" in c[1] and not c[1].endswith("format=json")]      # nothing downloaded


def test_record_fetch_without_specs_or_context(xnat, tmp_path, monkeypatch, caplog):
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); (inp / "a").write_text("a"); out = tmp_path / "out"
    assert prereq.main(["--input", str(inp), "--output", str(out)]) == 0                       # no specs: pass-through only
    assert (out / "a").exists() and json.loads((out / "prereq.json").read_text())["prerequisites"] == []
    assert handler.calls == []
    monkeypatch.setenv("XNW_PREREQ_X", "pipeline=qsiprep"); monkeypatch.delenv("PROC_SESSION_ID")
    with caplog.at_level(logging.ERROR):
        assert prereq.main(["--input", str(inp), "--output", str(tmp_path / "out2")]) == 2
    assert "XNAT context is incomplete" in caplog.text


CONTRACT_ENV = {"XNW_CARD_ID": "fake-recon", "XNW_CARD_REVISION": "0.1.0", "XNW_ANALYSIS_TYPE": "fake-reconstruction",
                "XNW_CONTAINER_IMAGE": "xnatworks/fake-tool:0.1", "XNW_CONTAINER_DIGEST": "sha256:" + "c" * 64,
                "XNW_OUTPUT_RESOURCE_LABEL": "XNW_FAKE_RECON", "PROC_PIPELINE_NAME": "fake-recon", "PROC_PIPELINE_VERSION": "0.1.0"}


def test_unmet_prerequisite_is_recorded_as_a_failed_record_with_the_reason(xnat, tmp_path, monkeypatch, caplog):
    """James, 2026-09-07: 'if criteria are missing, it's very ambiguous why' — the CS only says Failed (Setup)."""
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); out = tmp_path / "out"
    for k, v in CONTRACT_ENV.items(): monkeypatch.setenv(k, v)
    monkeypatch.setenv("XNW_PREREQ_PREPROC", "type=diffusion-preprocessing;pipeline=qsiprep;accepted=true;min=2.0")
    monkeypatch.setenv("XNW_PREREQ_BIDS", "resource=BIDS")
    with caplog.at_level(logging.INFO):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 3
    m = json.loads((out / "prereq.json").read_text())
    assert m["analysis_record"]["id"] == "XNAT_E77" and m["analysis_record"]["label"].startswith("fake-recon_S1_")
    assert m["analysis_record"]["uploaded"] == {"PROVENANCE": ["prereq.json"]}
    create = next(c for c in handler.calls if c[0] == "PUT" and "/out/" not in c[1])
    xml = create[3].decode()
    assert create[1].startswith("/data/experiments/XNAT_E1/assessors/fake-recon_S1_") and create[1].endswith("_record?inbody=true")
    assert "<analysis:run_status>FAILED</analysis:run_status>" in xml and "<analysis:auto_qc_status>FAIL</analysis:auto_qc_status>" in xml
    assert "<analysis:pipeline_name>fake-recon</analysis:pipeline_name>" in xml and "<analysis:analysis_type>fake-reconstruction</analysis:analysis_type>" in xml
    assert ("fake-recon 0.1.0 did not run on session S1: prerequisite &#x27;preproc&#x27; needs a diffusion-preprocessing record from pipeline qsiprep "
            "(version &gt;= 2.0, ACCEPTED in review, run SUCCEEDED), files from its DERIVED resource; no SUCCEEDED record at version &gt;= 2.0: XNAT_E12" in xml.replace("'", "&#x27;")
            or "fake-recon 0.1.0 did not run on session S1: prerequisite 'preproc' needs a diffusion-preprocessing record from pipeline qsiprep" in xml)
    assert "Nothing was computed; recorded at setup by record-fetch" in xml
    assert "<analysis:wrapup_version>record-fetch " in xml
    inputs = json.loads(html_unescape(xml.split("<analysis:inputs_json>")[1].split("</analysis:inputs_json>")[0]))
    assert inputs["stage"] == "setup" and [q["name"] for q in inputs["prerequisites"]] == ["bids", "preproc"]
    assert "no SUCCEEDED record at version >= 2.0" in inputs["prerequisites"][1]["error"]
    assert "failure recorded as analysis record XNAT_E77" in caplog.text
    assert handler.calls[-1][:2] == ("DELETE", "/data/JSESSION")


def test_failure_record_uses_the_seg_variables_then_the_contract_for_the_pipeline(xnat, tmp_path, monkeypatch):
    """Codex P2 (PR #10): a segmentation card sets SEG_MODEL_NAME/VERSION, not PROC_*; the failure
    record must name the same pipeline seg-wrapup would, and fall back to the card contract last."""
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir()
    for k, v in CONTRACT_ENV.items(): monkeypatch.setenv(k, v)
    monkeypatch.delenv("PROC_PIPELINE_NAME"); monkeypatch.delenv("PROC_PIPELINE_VERSION")
    monkeypatch.setenv("SEG_MODEL_NAME", "deepwmh"); monkeypatch.setenv("SEG_MODEL_VERSION", "1.0.1")
    monkeypatch.setenv("XNW_PREREQ_PREPROC", "pipeline=qsiprep;min=9.0")
    assert prereq.main(["--input", str(inp), "--output", str(tmp_path / "out")]) == 3
    xml = next(c for c in handler.calls if c[0] == "PUT" and "/out/" not in c[1])[3].decode()
    assert "<analysis:pipeline_name>deepwmh</analysis:pipeline_name>" in xml
    assert "<analysis:pipeline_version>1.0.1</analysis:pipeline_version>" in xml
    assert "deepwmh 1.0.1 did not run on session S1" in xml
    handler.calls.clear()
    monkeypatch.delenv("SEG_MODEL_NAME"); monkeypatch.delenv("SEG_MODEL_VERSION")
    assert prereq.main(["--input", str(inp), "--output", str(tmp_path / "out2")]) == 3
    xml = next(c for c in handler.calls if c[0] == "PUT" and "/out/" not in c[1])[3].decode()
    assert "<analysis:pipeline_name>fake-recon</analysis:pipeline_name>" in xml
    assert "<analysis:pipeline_version>0.1.0</analysis:pipeline_version>" in xml


def test_unmet_prerequisite_without_a_contract_records_nothing(xnat, tmp_path, monkeypatch, caplog):
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); out = tmp_path / "out"
    for k in CONTRACT_ENV: monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XNW_PREREQ_PREPROC", "pipeline=qsiprep;min=2.0")
    with caplog.at_level(logging.INFO):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 3
    assert json.loads((out / "prereq.json").read_text())["analysis_record"] is None
    assert not [c for c in handler.calls if c[0] == "PUT"]
    assert "not recorded as an analysis record" in caplog.text


# ── scan-level prerequisites (dcm2niix needs the scan's DICOM) ────────────────

def test_parse_scan_scope():
    p = Prerequisite.parse("DICOM", "resource=DICOM;scope=scan")
    assert (p.is_resource, p.scope, p.scan_type) == (True, "scan", "")
    assert Prerequisite.parse("T1", "resource=DICOM;scope=scan;scan_type=T1*").scan_type == "T1*"
    with pytest.raises(ValueError, match="scope must be session or subject for a record"):
        Prerequisite.parse("X", "pipeline=qsiprep;scope=scan")
    with pytest.raises(ValueError, match="scope must be session or scan for a resource"):
        Prerequisite.parse("X", "resource=DICOM;scope=subject")


def test_scan_scope_on_a_scan_level_run_takes_the_runs_scan(xnat, tmp_path, monkeypatch):
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); out = tmp_path / "out"
    monkeypatch.setenv("PROC_SCAN_ID", "2")
    monkeypatch.setenv("XNW_PREREQ_DICOM", "resource=DICOM;scope=scan")
    assert prereq.main(["--input", str(inp), "--output", str(out)]) == 0
    assert sorted(f.name for f in (out / "prereq" / "dicom" / "2").iterdir()) == ["1.dcm", "2.dcm"]
    m = json.loads((out / "prereq.json").read_text())["prerequisites"][0]
    assert m["kind"] == "scan-resource" and m["scans"] == ["2"] and m["files"] == 2 and m["resource"] == "DICOM"
    assert not [c for c in handler.calls if c[1].endswith("/scans?format=json")], "no scan listing needed on a scan-level run"
    assert not [c for c in handler.calls if "xsiType=analysis:sessionAnalysisData" in c[1]], "no record listing when no prerequisite is a record"


def test_scan_scope_on_a_session_level_run_selects_scans_by_type(xnat, tmp_path, monkeypatch, caplog):
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); out = tmp_path / "out"
    monkeypatch.delenv("PROC_SCAN_ID", raising=False)
    monkeypatch.setenv("XNW_PREREQ_BOLD", "resource=DICOM;scope=scan;scan_type=BOLD")
    assert prereq.main(["--input", str(inp), "--output", str(out)]) == 0
    assert (out / "prereq" / "bold" / "3" / "b1.dcm").read_bytes() == b"data:b1.dcm"
    assert json.loads((out / "prereq.json").read_text())["prerequisites"][0]["scans"] == ["3"]
    # T1* matches scans 2 and 4; scan 4 has an empty DICOM resource, which is a reason, not a silent gap
    monkeypatch.setenv("XNW_PREREQ_BOLD", "resource=DICOM;scope=scan;scan_type=T1*")
    with caplog.at_level(logging.ERROR):
        assert prereq.main(["--input", str(inp), "--output", str(tmp_path / "out2")]) == 3
    assert "prerequisite bold: needs the DICOM resource of scans of type 'T1*'; scan 4 has no files in its DICOM resource" in caplog.text
    # no scan of that type at all: named, with what the session does have
    monkeypatch.setenv("XNW_PREREQ_BOLD", "resource=DICOM;scope=scan;scan_type=DWI")
    with caplog.at_level(logging.ERROR):
        assert prereq.main(["--input", str(inp), "--output", str(tmp_path / "out3")]) == 3
    assert "needs the DICOM resource of scans of type 'DWI'; no scan of type 'DWI' on this session (scan types present: ['BOLD', 'T1w'])" in caplog.text
    # scope=scan with neither a scan-level run nor a type filter is refused before any request
    monkeypatch.setenv("XNW_PREREQ_BOLD", "resource=DICOM;scope=scan")
    with caplog.at_level(logging.ERROR):
        assert prereq.main(["--input", str(inp), "--output", str(tmp_path / "out4")]) == 3
    assert "needs the DICOM resource of the run's scan; scope=scan needs a scan-level run (PROC_SCAN_ID) or scan_type=<glob> in the card" in caplog.text


def test_a_missing_resource_is_an_unmet_prerequisite_with_a_record_not_a_transport_error(xnat, tmp_path, monkeypatch, caplog):
    """Codex P2 on PR #10: XNAT answers 404 for a resource that does not exist; that is a reason, not exit 2."""
    host, handler = xnat
    inp = tmp_path / "in"; inp.mkdir(); out = tmp_path / "out"
    for k, v in CONTRACT_ENV.items(): monkeypatch.setenv(k, v)
    monkeypatch.setenv("XNW_PREREQ_FMAP", "resource=FIELDMAPS")
    with caplog.at_level(logging.ERROR):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 3
    m = json.loads((out / "prereq.json").read_text())
    assert m["prerequisites"][0]["error"] == "needs the session resource FIELDMAPS; the FIELDMAPS resource does not exist on this session (XNAT answered 404)"
    assert m["analysis_record"]["id"] == "XNAT_E77"
    assert "prerequisite fmap: needs the session resource FIELDMAPS; the FIELDMAPS resource does not exist" in caplog.text


# ── scope rules (0.6.2) ──────────────────────────────────────────────────────

def _read(out, rel):
    return (out / "prereq" / rel).read_bytes()


def test_a_session_run_falls_back_to_the_subjects_record_when_no_session_record_fits(xnat, tmp_path, monkeypatch, caplog):
    """xcp-d on ses-preop after a subject-scoped fMRIPrep: the session has no fmriprep record,
    the subject does, and it covers the session."""
    host, server = xnat
    inp, out = tmp_path / "in", tmp_path / "out"; inp.mkdir()
    monkeypatch.setenv("XNW_PREREQ_PREPROC", "pipeline=fmriprep;accepted=true;role=DERIVED")
    with caplog.at_level(logging.INFO):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "prereq.json").read_text())
    got = manifest["prerequisites"][0]
    assert got["record"]["ID"] == "XNAT_E50" and got["record"]["scope"] == "subject" and got["files"] == 2
    assert _read(out, "preproc/sub-292/ses-preop/func/bold.nii.gz") == b"data:bold.nii.gz"
    assert "using subject record XNAT_E50" in caplog.text
    paths = [c[1] for c in server.calls if c[0] == "GET"]
    assert "/data/experiments/XNAT_E1?format=json" in paths                                    # the session's subject
    assert any(p.startswith("/data/experiments/XNAT_E50/resources/DERIVED/files?format=json") for p in paths)   # experiment resources, no /out/


def test_scope_subject_prerequisite_on_a_session_run_looks_only_at_the_subject(xnat, tmp_path, monkeypatch):
    host, server = xnat
    inp, out = tmp_path / "in", tmp_path / "out"; inp.mkdir()
    monkeypatch.setenv("XNW_PREREQ_QSIPREP", "pipeline=qsiprep;scope=subject")      # the subject has no qsiprep record
    assert prereq.main(["--input", str(inp), "--output", str(out)]) == 3
    got = json.loads((out / "prereq.json").read_text())["prerequisites"][0]
    assert "subject XNAT_S1" in got["error"] and "a subject record from pipeline qsiprep" in got["error"]
    assert not any(c[0] == "GET" and "xsiType=analysis:sessionAnalysisData" in c[1] for c in server.calls)


def _subject_run(monkeypatch):
    monkeypatch.delenv("PROC_SESSION_ID"); monkeypatch.setenv("PROC_SUBJECT_ID", "XNAT_S1")


def test_a_subject_run_needs_the_session_prerequisite_on_every_session_and_lays_them_out_by_label(xnat, tmp_path, monkeypatch):
    host, server = xnat
    _subject_run(monkeypatch)
    inp, out = tmp_path / "in", tmp_path / "out"; inp.mkdir()
    monkeypatch.setenv("XNW_PREREQ_QSIPREP", "pipeline=qsiprep;accepted=true;role=DERIVED")
    monkeypatch.setenv("XNW_PREREQ_BIDS", "resource=BIDS")
    assert prereq.main(["--input", str(inp), "--output", str(out)]) == 0
    manifest = json.loads((out / "prereq.json").read_text())
    by_name = {q["name"]: q for q in manifest["prerequisites"]}
    assert by_name["qsiprep"]["sessions"] == {"S1": {"ID": "XNAT_E11", "label": "qsiprep_S1_20260907T110000Z_record", "pipeline_name": "qsiprep",
                                                     "pipeline_version": "1.1.1", "review_state": "ACCEPTED", "run_status": "SUCCEEDED"},
                                              "S2": {"ID": "XNAT_E99", "label": "qsiprep_other_session", "pipeline_name": "qsiprep",
                                                     "pipeline_version": "1.1.1", "review_state": "ACCEPTED", "run_status": "SUCCEEDED"}}
    assert by_name["qsiprep"]["record"]["ID"] == "XNAT_E99" and by_name["qsiprep"]["files"] == 2       # newest of the two
    assert _read(out, "qsiprep/S1/sub-1/dwi/preproc.nii.gz") == b"data:preproc.nii.gz"
    assert _read(out, "qsiprep/S2/sub-1/dwi/preproc.nii.gz") == b"data:preproc.nii.gz"
    assert by_name["bids"]["files"] == 4 and _read(out, "bids/S2/dataset_description.json") == b"data:dataset_description.json"
    listed = [c[1] for c in server.calls if c[0] == "GET" and "/subjects/XNAT_S1/experiments?format=json" in c[1]]
    assert len(listed) == 1, "the subject's sessions are listed once for the whole run"


def test_a_subject_run_reports_which_sessions_lack_the_prerequisite_and_records_the_failure_on_the_subject(xnat, tmp_path, monkeypatch, caplog):
    host, server = xnat
    _subject_run(monkeypatch)
    for k, v in CONTRACT_ENV.items(): monkeypatch.setenv(k, v)
    inp, out = tmp_path / "in", tmp_path / "out"; inp.mkdir()
    monkeypatch.setenv("XNW_PREREQ_QC", "pipeline=mriqc;role=PROVENANCE")           # only S1 has an mriqc record
    with caplog.at_level(logging.ERROR):
        assert prereq.main(["--input", str(inp), "--output", str(out)]) == 3
    manifest = json.loads((out / "prereq.json").read_text())
    got = manifest["prerequisites"][0]
    assert "on every session of subject XNAT_S1; 1 of 2 unmet: S2:" in got["error"] and "S1" not in got["error"].split("unmet:")[1].split(":")[0]
    assert not (out / "prereq" / "qc").exists() or not any((out / "prereq" / "qc").rglob("*")), "nothing is downloaded for an unmet prerequisite"
    creates = [c for c in server.calls if c[0] == "PUT" and "/subjects/XNAT_S1/experiments/" in c[1] and "?inbody=true" in c[1]]
    assert len(creates) == 1 and b"<analysis:SubjectAnalysis" in creates[0][3] and b"<xnat:subject_ID>XNAT_S1</xnat:subject_ID>" in creates[0][3]
    assert b"did not run on subject 292" in creates[0][3]
    assert manifest["analysis_record"]["id"] == "XNAT_E77" and manifest["analysis_record"]["xsi_type"] == "analysis:subjectAnalysisData"
    # the failure record names its scope, subject and sessions like any subject record (Codex on PR #15)
    body = creates[0][3].decode()
    inputs = json.loads(html.unescape(body.split("<analysis:inputs_json>")[1].split("</analysis:inputs_json>")[0]))
    assert inputs["scope"] == "subject" and inputs["subject"] == "XNAT_S1" and inputs["stage"] == "setup"
    assert inputs["sessions"] == [{"ID": "XNAT_E1", "label": "S1"}, {"ID": "XNAT_E2", "label": "S2"}]


def test_a_subject_run_finds_a_subject_scoped_prerequisite_on_the_subject_itself(xnat, tmp_path, monkeypatch):
    host, server = xnat
    _subject_run(monkeypatch)
    inp, out = tmp_path / "in", tmp_path / "out"; inp.mkdir()
    monkeypatch.setenv("XNW_PREREQ_PREPROC", "pipeline=fmriprep;scope=subject;accepted=true")
    assert prereq.main(["--input", str(inp), "--output", str(out)]) == 0
    got = json.loads((out / "prereq.json").read_text())["prerequisites"][0]
    assert got["record"]["ID"] == "XNAT_E50" and got["files"] == 2 and "sessions" not in got


def test_a_subject_run_refuses_a_scan_resource_prerequisite(xnat, tmp_path, monkeypatch):
    host, server = xnat
    _subject_run(monkeypatch)
    inp, out = tmp_path / "in", tmp_path / "out"; inp.mkdir()
    monkeypatch.setenv("XNW_PREREQ_DICOM", "resource=DICOM;scope=scan;scan_type=T1*")
    assert prereq.main(["--input", str(inp), "--output", str(out)]) == 3
    got = json.loads((out / "prereq.json").read_text())["prerequisites"][0]
    assert "cannot be gathered for a subject-scoped run" in got["error"]
