"""Diagnostic report records (0.7.2): a card's FHIR R5 DiagnosticReport filed as a dxreport:sessionReportData."""
import base64
import json
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from segwrapup import dxreport
from segwrapup.register import XnatContext

DX = "{http://xnatworks.io/dxreport}"
REPORT_TEXT = "TECHNIQUE: CT chest.\nFINDINGS:\nLungs: Filling defects in both main pulmonary arteries.\nIMPRESSION:\n1. Bilateral pulmonary emboli."


def card_report(**overrides):
    """A report shaped like the one the NV-Reason-CT card writes: the model is a contained Device, and the
    performer references it (with a display, which must not make it a human)."""
    doc = {
        "resourceType": "DiagnosticReport", "id": "3f2a9c1d0b7e4a65", "meta": {"versionId": "1"},
        "contained": [
            {"resourceType": "Device", "id": "model", "name": [{"value": "NVIDIA NV-Reason-CT", "type": "user-friendly-name"}],
             "version": [{"value": "386b93e"}]},
            {"resourceType": "ImagingStudy", "id": "study", "status": "available",
             "identifier": [{"system": "urn:dicom:uid", "value": "urn:oid:1.2.3.4"}]},
            {"resourceType": "Observation", "id": "finding-1", "status": "preliminary", "code": {"text": "Lungs"},
             "valueString": "Filling defects in both main pulmonary arteries."},
            {"resourceType": "Observation", "id": "finding-2", "status": "preliminary", "code": {"text": "Direct question"},
             "valueString": "Yes", "note": [{"text": "Is a pulmonary embolism present in this CT?"}]},
        ],
        "status": "preliminary",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "RAD"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "68604-8", "display": "Radiology Diagnostic study note"}]},
        "issued": "2026-10-05T14:03:12Z",
        "performer": [{"reference": "#model", "display": "NVIDIA NV-Reason-CT"}],
        "result": [{"reference": "#finding-1"}, {"reference": "#finding-2"}],
        "conclusion": "Bilateral pulmonary emboli.",
        "presentedForm": [{"contentType": "text/plain; charset=utf-8", "data": base64.b64encode(REPORT_TEXT.encode()).decode()}],
    }
    doc.update(overrides)
    return doc


FACTS = {"id": "XNAT_E00026", "label": "HB0004_1", "project": "RSNA_DEMO", "uid": "1.2.840.99.7", "modality": "CT", "subject_label": "RSNAPID001"}


# ── parsing ────────────────────────────────────────────────────────────────────
def test_a_card_report_parses_as_an_ai_draft_with_its_findings_and_verbatim_text():
    parsed = dxreport.parse(card_report())
    assert (parsed["id"], parsed["status"], parsed["category"], parsed["code"]) == ("3f2a9c1d0b7e4a65", "preliminary", "RAD", "68604-8")
    assert (parsed["device_name"], parsed["device_version"], parsed["performer"]) == ("NVIDIA NV-Reason-CT", "386b93e", None)
    assert dxreport.report_kind(parsed) == "AI_DRAFT", "a performer that is the contained Device is the model, not a person"
    assert parsed["narrative"] == REPORT_TEXT and parsed["study_instance_uid"] == "1.2.3.4"
    assert [(f["display"], f["value_string"], f["text"]) for f in parsed["findings"]] == [
        ("Lungs", "Filling defects in both main pulmonary arteries.", None), ("Direct question", "Yes", None)]
    assert parsed["simulated"] is False


def test_the_report_kind_follows_the_poller():
    human = dxreport.parse(card_report(performer=[{"display": "Dr Example"}], status="final"))
    assert human["performer"] == "Dr Example" and dxreport.report_kind(human) == "FINAL_SIGNED"
    assert dxreport.report_kind(dxreport.parse(card_report(performer=[{"display": "Dr Example"}]))) == "HUMAN_DRAFT"
    practitioner = card_report(performer=[{"reference": "#dr"}])
    practitioner["contained"].append({"resourceType": "Practitioner", "id": "dr", "name": [{"text": "Dr Contained"}]})
    assert dxreport.parse(practitioner)["performer"] == "Dr Contained"


def test_findings_fall_back_to_every_contained_observation_and_read_quantities_and_components():
    doc = card_report(result=[])
    doc["contained"].append({"resourceType": "Observation", "id": "v", "code": {"coding": [{"system": "s", "code": "c", "display": "Clot volume"}], "text": "volume of clot"},
                             "valueQuantity": {"value": 12.5, "unit": "mL"}, "bodySite": {"coding": [{"display": "Right PA"}]},
                             "component": [{"code": {"text": "Confidence"}, "valueQuantity": {"value": 0.9}},
                                           {"code": {"text": "Instance number"}, "valueInteger": 42}]})
    rows = dxreport.parse(doc)["findings"]
    assert len(rows) == 3, "no result references: every contained Observation"
    assert rows[2] == {"code_system": "s", "code": "c", "display": "Clot volume", "text": "volume of clot", "value_string": None,
                       "value_number": 12.5, "unit": "mL", "confidence": 0.9, "body_site": "Right PA", "instance_number": 42}


def test_a_simulated_tag_and_a_narrative_from_xhtml():
    doc = card_report(meta={"versionId": "2", "tag": [{"code": "Simulated"}]}, presentedForm=[], text={"div": "<div><p>Lungs clear.</p></div>"})
    parsed = dxreport.parse(doc)
    assert parsed["simulated"] is True and parsed["version_id"] == "2" and parsed["narrative"] == "Lungs clear."


def test_not_a_report_or_no_id_is_refused():
    with pytest.raises(dxreport.NotADiagnosticReport, match="expected a DiagnosticReport"):
        dxreport.parse({"resourceType": "Observation"})
    with pytest.raises(dxreport.NotADiagnosticReport, match="no id"):
        dxreport.parse(card_report(id=""))
    with pytest.raises(dxreport.NotADiagnosticReport):
        dxreport.parse(["not", "a", "dict"])


def test_a_bad_attachment_costs_the_narrative_only(caplog):
    parsed = dxreport.parse(card_report(presentedForm=[{"data": "@@not base64@@"}]))
    assert parsed["narrative"] is None and parsed["conclusion"] == "Bilateral pulmonary emboli."
    assert "not base64" in caplog.text


def _form(content_type, payload: bytes) -> dict:
    return {"contentType": content_type, "data": base64.b64encode(payload).decode()}


def test_a_pdf_or_undecodable_attachment_is_passed_over_for_the_next_text(caplog):
    """Codex P2, PR #23: a PDF first in presentedForm left the narrative empty."""
    pdf = _form("application/pdf", b"%PDF-1.7\n\xe2\xe3\xcf\xd3")
    not_utf8 = _form("text/plain", "caf\xe9".encode("latin-1"))
    unknown_charset = _form("text/plain; charset=x-nonesuch", b"text")
    text = _form("text/plain; charset=utf-8", REPORT_TEXT.encode())
    assert dxreport.parse(card_report(presentedForm=[pdf, not_utf8, unknown_charset, text]))["narrative"] == REPORT_TEXT
    # a declared charset is honoured
    latin = _form('text/plain; charset="ISO-8859-1"', "caf\xe9".encode("latin-1"))
    assert dxreport.parse(card_report(presentedForm=[latin, text]))["narrative"] == "caf\xe9"
    # nothing textual among the attachments: the XHTML narrative is the fallback
    div = {"div": "<div><p>Lungs clear.</p></div>"}
    assert dxreport.parse(card_report(presentedForm=[pdf, {"data": "@@"}], text=div))["narrative"] == "Lungs clear."
    # control: the first text attachment is still the one taken
    other = _form("text/plain", b"Other text.")
    assert dxreport.parse(card_report(presentedForm=[text, other]))["narrative"] == REPORT_TEXT
    # an attachment with no contentType is tried as text, as before
    assert dxreport.parse(card_report(presentedForm=[{"data": base64.b64encode(b"Untyped.").decode()}]))["narrative"] == "Untyped."


# ── the record ─────────────────────────────────────────────────────────────────
def test_the_label_is_the_pollers():
    assert dxreport.record_label("HB0004_1", "urn:xnatworks:card:nv-reason-ct", "3f2a9c1d0b7e4a65") == "HB0004_1_RPT_nv-reason-ct_3f2a9c1d0b7e4a65"
    assert dxreport.record_label("S", "https://fhir.example.org/fhir/", "r1") == "S_RPT_fhir_r1"
    assert len(dxreport.record_label("L" * 60, "urn:x:card", "id")) == dxreport.LABEL_MAX


def test_values_name_the_archived_session_and_clamp_what_the_schema_would_refuse():
    parsed = dxreport.parse(card_report(status="Draft-ish", category=[{"coding": [{"code": "XYZ"}]}], conclusion="c" * 9000))
    values = dxreport.build_values(parsed, json.dumps(card_report()), FACTS, "urn:xnatworks:card:nv-reason-ct", "None needed")
    assert (values["project"], values["session_id"], values["pseudonym"], values["modality"]) == ("RSNA_DEMO", "XNAT_E00026", "RSNAPID001", "CT")
    assert values["study_instance_uid"] == "1.2.840.99.7", "the archive's UID, not the one the document claims"
    assert (values["report_status"], values["category"]) == ("unknown", "OTH"), "outside the XSD's enumerations"
    assert len(values["conclusion"]) == dxreport.CONCLUSION_MAX
    assert (values["source_format"], values["source_id"], values["source_version"]) == ("FHIR_R5", "3f2a9c1d0b7e4a65", "1")
    huge = dxreport.build_values(parsed, "x" * (dxreport.RAW_JSON_MAX + 1), FACTS, "urn:x", "n")
    assert huge["raw_json"] is None, "over the cap: the document stays whole on the REPORT resource instead"


def test_a_date_without_a_time_is_left_off():
    values = dxreport.build_values(dxreport.parse(card_report(issued="2026-10-05", effectiveDateTime="2026-10-05T14:00:00+01:00")),
                                   "{}", FACTS, "urn:x", "n")
    assert values["issued"] is None and values["effective"] == "2026-10-05T14:00:00+01:00"


XSD_ORDER = ["imageSession_ID", "category", "code_system", "code", "code_display", "report_status", "report_kind", "source_system",
             "source_format", "source_id", "source_version", "issued", "effective", "performer", "device_name", "device_version",
             "simulated", "pseudonym", "conclusion", "narrative", "finding_count", "findings", "review_state", "reviewed_by",
             "reviewed_timestamp", "review_reason", "pseudonymization", "raw_json", "modality", "study_instance_uid"]


def test_the_xml_parses_follows_the_xsd_sequence_and_escapes_the_text():
    doc = card_report(conclusion='Emboli <large> & "saddle"')
    values = dxreport.build_values(dxreport.parse(doc), json.dumps(doc), FACTS, "urn:xnatworks:card:nv-reason-ct", "None needed")
    root = ElementTree.fromstring(dxreport.build_record_xml(values).encode())
    assert root.tag == DX + "SessionReport" and root.get("label") == "HB0004_1_RPT_nv-reason-ct_3f2a9c1d0b7e4a65" and root.get("project") == "RSNA_DEMO"
    names = [child.tag.split("}")[1] for child in root]
    assert names == [n for n in XSD_ORDER if n in names], "elements in the XSD's sequence order"
    assert root.find(DX + "conclusion").text == 'Emboli <large> & "saddle"'
    assert root.find(DX + "simulated").text == "false" and root.find(DX + "finding_count").text == "2"
    assert json.loads(root.find(DX + "raw_json").text)["id"] == "3f2a9c1d0b7e4a65"
    rows = root.find(DX + "findings").findall(DX + "finding")
    assert [r.find(DX + "display").text for r in rows] == ["Lungs", "Direct question"]


def test_the_declared_path_must_stay_inside_the_tools_output(tmp_path):
    assert dxreport.declared_path(tmp_path, "raw", "reports/dr.json") == tmp_path / "raw" / "reports" / "dr.json"
    for bad in ("", "/etc/passwd", "../outside.json", "a/../../b.json"):
        with pytest.raises(ValueError):
            dxreport.declared_path(tmp_path, "raw", bad)


# ── publishing (XNAT faked at the module boundary) ─────────────────────────────
def context(scope="session"):
    return XnatContext(host="http://x", user="u", password="p", project="RSNA_DEMO", session="XNAT_E00026" if scope == "session" else "",
                       subject="XNAT_S1" if scope == "subject" else "", dataset="XNAT_D1" if scope == "dataset" else "")


def written(tmp_path, doc=None):
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    (raw / "diagnostic_report.json").write_text(json.dumps(doc if doc is not None else card_report()))
    return tmp_path


@pytest.fixture
def xnat(monkeypatch):
    calls = {"published": [], "probe": 404,
             # what a record already under the label says about itself, read when the probe finds one
             "there": {"source_id": "3f2a9c1d0b7e4a65", "source_system": "urn:xnatworks:card:nv-reason-ct"}}
    monkeypatch.setattr(dxreport, "fetch_session_facts", lambda ctx, timeout: dict(FACTS))
    monkeypatch.setattr(dxreport, "_request", lambda ctx, method, url, timeout: calls["probe"])
    monkeypatch.setattr(dxreport, "_get_json", lambda ctx, url, timeout: {"items": [{"data_fields": calls["there"]}]})

    def publish(ctx, label, xml, files, timeout_seconds=300.0, xsi_type=None, retry_on_conflict=True):
        if calls.get("conflict"):
            raise RuntimeError("PUT %s failed: HTTP 409 Conflict" % label)
        if calls.get("inner_probe_taken"):           # publish_record's own message for a label its probe finds
            raise RuntimeError(f"label {label} already exists on session {ctx.session}; the record is create-only, "
                               "pass a fresh --record-label or let the run stamp one")
        calls["published"].append({"label": label, "xml": xml, "files": files, "xsi_type": xsi_type,
                                   "retry_on_conflict": retry_on_conflict})
        return {"id": "XNAT_E99999", "label": label}
    monkeypatch.setattr(dxreport, "publish_record", publish)
    return calls


CONTRACT = {"card_id": "nv-reason-ct", "card_revision": "0.6.0"}


def test_a_succeeded_session_run_files_the_report_with_the_document_on_report(tmp_path, xnat):
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out == {"xsi_type": "dxreport:sessionReportData", "id": "XNAT_E99999", "label": "HB0004_1_RPT_nv-reason-ct_3f2a9c1d0b7e4a65",
                   "source_id": "3f2a9c1d0b7e4a65", "report_kind": "AI_DRAFT", "findings": 2, "file": "diagnosticreport-3f2a9c1d0b7e4a65-v1.json"}
    (call,) = xnat["published"]
    assert call["xsi_type"] == "dxreport:sessionReportData"
    (item,) = call["files"]["REPORT"]
    assert item.name == "diagnosticreport-3f2a9c1d0b7e4a65-v1.json" and item.path == tmp_path / "raw" / "diagnostic_report.json"
    root = ElementTree.fromstring(call["xml"].encode())
    assert root.find(DX + "source_system").text == "urn:xnatworks:card:nv-reason-ct"
    assert "card nv-reason-ct 0.6.0" in root.find(DX + "pseudonymization").text


def test_the_same_report_is_not_filed_twice(tmp_path, xnat):
    xnat["probe"] = 200
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out["exists"] is True and xnat["published"] == []


@pytest.mark.parametrize("scope", ["subject", "dataset"])
def test_only_a_session_run_files_one(tmp_path, xnat, scope):
    out = dxreport.publish_diagnostic_report(context(scope), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert "session" in out["skipped"] and xnat["published"] == []


def test_nothing_from_a_failed_run_a_missing_file_or_without_xnat(tmp_path, xnat):
    assert dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "FAILED", CONTRACT) == {"skipped": "run FAILED"}
    assert "no other.json" in dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "other.json", "SUCCEEDED", CONTRACT)["skipped"]
    assert dxreport.publish_diagnostic_report(None, tmp_path, "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT) == {"skipped": "no XNAT context"}
    assert xnat["published"] == []


def test_a_bad_document_or_a_failed_create_is_an_outcome_not_an_abort(tmp_path, xnat, monkeypatch, caplog):
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path, {"resourceType": "Bundle"}), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out["error"].startswith("NotADiagnosticReport")
    (tmp_path / "raw" / "diagnostic_report.json").write_text("{not json")
    assert "JSONDecodeError" in dxreport.publish_diagnostic_report(context(), tmp_path, "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)["error"]
    escaped = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "../escape.json", "SUCCEEDED", CONTRACT)
    assert escaped["error"].startswith("ValueError") and "inside the tool's output" in escaped["error"]
    monkeypatch.setattr(dxreport, "publish_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PUT failed: HTTP 500 boom")))
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out == {"error": "RuntimeError: PUT failed: HTTP 500 boom"}
    assert "not filed" in caplog.text


def test_a_409_retry_relabels_the_report_root_so_the_document_matches_the_url():
    from segwrapup.publish import _relabel
    values = dxreport.build_values(dxreport.parse(card_report()), "{}", FACTS, "urn:xnatworks:card:nv-reason-ct", "n")
    root = ElementTree.fromstring(_relabel(dxreport.build_record_xml(values), "HB0004_1_RPT_nv-reason-ct_3f2a_9c1d").encode())
    assert root.get("label") == "HB0004_1_RPT_nv-reason-ct_3f2a_9c1d"


def test_the_contract_carries_the_declaration_from_env_and_from_json():
    from segwrapup.publish import RecordContract
    assert RecordContract.from_env({"XNW_CARD_ID": "nv-reason-ct", "XNW_DIAGNOSTIC_REPORT": "diagnostic_report.json"}).diagnostic_report == "diagnostic_report.json"
    assert RecordContract.from_env({"XNW_CARD_ID": "nv-reason-ct"}).diagnostic_report == ""
    as_json = json.dumps({"card_id": "nv-reason-ct", "diagnostic_report": "reports/dr.json"})
    assert RecordContract.from_env({"XNW_CONTRACT": as_json}).diagnostic_report == "reports/dr.json"


# ── Codex round 1 on PR #23 ────────────────────────────────────────────────────
def test_only_the_device_a_performer_references_makes_an_ai_draft():
    scanner = {"resourceType": "Device", "id": "scanner", "name": [{"value": "CT scanner"}], "version": [{"value": "VB20"}]}
    human_with_scanner = card_report(performer=[], contained=[scanner] + card_report()["contained"][1:])
    parsed = dxreport.parse(human_with_scanner)
    assert (parsed["device_name"], dxreport.report_kind(parsed)) == (None, "HUMAN_DRAFT"), "a scanner in the report wrote nothing"
    second = card_report()
    second["contained"].insert(0, scanner)                          # the model is now the second Device
    parsed = dxreport.parse(second)
    assert (parsed["device_name"], parsed["device_version"], dxreport.report_kind(parsed)) == ("NVIDIA NV-Reason-CT", "386b93e", "AI_DRAFT")


def test_a_long_session_label_keeps_the_report_identity_in_the_label():
    long_label = "S" * 60
    first = dxreport.record_label(long_label, "urn:xnatworks:card:nv-reason-ct", "3f2a9c1d0b7e4a65")
    second = dxreport.record_label(long_label, "urn:xnatworks:card:nv-reason-ct", "0000000000000001")
    other_card = dxreport.record_label(long_label, "urn:xnatworks:card:hoppr", "3f2a9c1d0b7e4a65")
    assert len({first, second, other_card}) == 3 and all(len(x) <= dxreport.LABEL_MAX for x in (first, second, other_card))
    assert first.endswith("_RPT_nv-reason-ct_3f2a9c1d0b7e4a65")
    # two long session labels that share their first 60 characters stay apart
    assert dxreport.record_label(long_label + "_A", "urn:x:card", "r") != dxreport.record_label(long_label + "_B", "urn:x:card", "r")
    # a report id too long for the label is replaced by its hash, still distinct and still under the cap
    a, b = (dxreport.record_label("HB0004_1", "urn:x:card", "x" * 80 + tail) for tail in ("1", "2"))
    assert a != b and len(a) <= dxreport.LABEL_MAX and a.startswith("HB0004_1_RPT_card_")
    # the common case is still the poller's label, unchanged
    assert dxreport.record_label("HB0004_1", "urn:xnatworks:card:nv-reason-ct", "3f2a9c1d0b7e4a65") == "HB0004_1_RPT_nv-reason-ct_3f2a9c1d0b7e4a65"


def test_a_character_xml_cannot_carry_is_written_as_its_escape_and_the_document_still_parses(caplog):
    doc = card_report(conclusion="Emboli\u0001 present￾")
    doc["contained"][2]["valueString"] = "Filling\u0002defects"
    values = dxreport.build_values(dxreport.parse(doc), json.dumps(doc, ensure_ascii=False), FACTS, "urn:xnatworks:card:nv-reason-ct", "n")
    root = ElementTree.fromstring(dxreport.build_record_xml(values).encode())
    assert root.find(DX + "conclusion").text == "Emboli\\x01 present\\ufffe"
    assert root.find(DX + "findings")[0].find(DX + "value_string").text == "Filling\\x02defects"
    assert "XML cannot carry" in caplog.text


@pytest.mark.parametrize("broken, outcome", [
    ({"presentedForm": ["oops"]}, "AttributeError"),
    ({"result": [{"reference": "#finding-1"}], "contained": [{"resourceType": "Observation", "id": "finding-1", "code": "Lungs"}]},
     "AttributeError"),
    ({"performer": ["not a reference"], "contained": ["not a resource"]}, "filed"),      # tolerated: no device, no findings
    ({"meta": {"versionId": "1", "tag": "simulated"}}, "filed"),                          # a tag that is not a list is no tag
    ({"category": "RAD"}, "filed"),                                                       # no coding: the default category
])
def test_a_malformed_nested_value_is_an_outcome_not_an_abort(tmp_path, xnat, broken, outcome, caplog):
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path, card_report(**broken)), "raw", "diagnostic_report.json",
                                             "SUCCEEDED", CONTRACT)
    if outcome == "filed":
        assert out["id"] == "XNAT_E99999"
    else:
        assert out["error"].startswith(outcome) and xnat["published"] == [] and "not filed" in caplog.text


def test_the_nv_reason_ct_cards_own_document_files_as_an_ai_draft_with_every_finding():
    """What the NV-Reason-CT card 0.6.0 driver writes for the RSNA demo CTPA fixture (container-workshop
    wrappers/nv-reason-ct/image, run on its test fixtures): the contract between the card and this module."""
    doc = json.loads((Path(__file__).parent / "fixtures" / "nv-reason-ct-0.6.0-diagnostic-report.json").read_text())
    parsed = dxreport.parse(doc)
    values = dxreport.build_values(parsed, json.dumps(doc), FACTS, "urn:xnatworks:card:nv-reason-ct", "n")
    assert (values["report_kind"], values["report_status"], values["category"], values["code"]) == ("AI_DRAFT", "preliminary", "RAD", "68604-8")
    assert values["device_name"] == "NVIDIA NV-Reason-CT" and values["device_version"] == "386b93e034983f6c1fc841a43833a1b6a0cd9c13"
    assert values["label"] == "HB0004_1_RPT_nv-reason-ct_" + doc["id"]
    assert len(values["findings"]) == 13 and values["findings"][-1]["display"] == "Direct question"
    assert values["conclusion"].startswith("1. Probable pulmonary emboli.") and "Research and education use only" in values["conclusion"]
    assert values["narrative"].startswith("TECHNIQUE: IV contrast CT.") and "</think>" not in values["narrative"]
    ElementTree.fromstring(dxreport.build_record_xml(values).encode())



# ── Codex round 2 on PR #23 ────────────────────────────────────────────────────
def test_a_number_too_large_for_a_float_is_left_off_not_raised(tmp_path, xnat, caplog):
    doc = card_report()
    doc["contained"][2]["valueQuantity"] = {"value": 10 ** 400, "unit": "mL"}
    assert dxreport._number(10 ** 400) is None and dxreport._number(12) == 12.0 and dxreport._number(True) is None
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path, doc), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out["id"] == "XNAT_E99999" and "too large" in caplog.text


def test_report_ids_the_label_cannot_spell_as_they_are_stay_distinct():
    labels = {rid: dxreport.record_label("HB0004_1", "urn:xnatworks:card:nv-reason-ct", rid) for rid in ("a-b", "a.b", "a_b", "ab")}
    assert len(set(labels.values())) == 4, labels
    assert labels["ab"] == "HB0004_1_RPT_nv-reason-ct_ab", "letters and digits are carried as they are"
    assert labels["a-b"] == "HB0004_1_RPT_nv-reason-ct_a-b", "a hyphen is a label character: carried as it is"
    assert re.fullmatch(r"HB0004_1_RPT_nv-reason-ct_a_b_[0-9a-f]{8}", labels["a.b"]), "rewritten, with a hash of the real id"
    # a UUID's readable prefix and hash would make the end over half the label: the id is hashed whole instead
    uuids = [dxreport.record_label("HB0004_1", "urn:x:card", u) for u in ("3fa85f64-5717-4562-b3fc-2c963f66afa6",
                                                                          "3fa85f64-5717-4562-b3fc-2c963f66afa7")]
    assert uuids[0] != uuids[1] and all(re.fullmatch(r"HB0004_1_RPT_card_[0-9a-f]{16}", u) for u in uuids), uuids


def test_an_unnamed_device_the_performer_references_is_still_the_author():
    doc = card_report()
    del doc["contained"][0]["name"]
    parsed = dxreport.parse(doc)
    assert parsed["device_name"] is None and dxreport.report_kind(parsed) == "AI_DRAFT"


def test_a_label_holding_another_report_is_a_collision_not_already_filed(tmp_path, xnat, caplog):
    xnat["probe"] = 200
    xnat["there"] = {"source_id": "something-else", "source_system": "urn:xnatworks:card:nv-reason-ct"}
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out == {"error": "label HB0004_1_RPT_nv-reason-ct_3f2a9c1d0b7e4a65 is taken by report 'something-else' from "
                            "'urn:xnatworks:card:nv-reason-ct'"}
    assert xnat["published"] == [] and "not filed" in caplog.text



# ── Codex round 3 on PR #23 ────────────────────────────────────────────────────
def test_session_labels_that_rewrite_alike_stay_apart():
    labels = [dxreport.record_label(session, "urn:x:card", "r1") for session in ("SUB-01", "SUB.01", "SUB_01", "SUB 01")]
    assert len(set(labels)) == 4, labels
    assert labels[0] == "SUB-01_RPT_card_r1" and labels[2] == "SUB_01_RPT_card_r1", "labels XNAT takes as they are stay as they are"
    assert all(len(label) <= dxreport.LABEL_MAX and re.fullmatch(r"[A-Za-z0-9_-]+", label) for label in labels), labels


def test_a_truncated_xnat_answer_is_an_outcome_not_an_abort(tmp_path, xnat, monkeypatch, caplog):
    import http.client

    def truncated(ctx, timeout):
        raise http.client.IncompleteRead(b"partial", 4096)
    monkeypatch.setattr(dxreport, "fetch_session_facts", truncated)
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out["error"].startswith("IncompleteRead") and xnat["published"] == [] and "not filed" in caplog.text


def test_a_document_nested_past_the_recursion_limit_is_an_outcome_not_an_abort(tmp_path, xnat, caplog):
    path = tmp_path / "raw"
    path.mkdir()
    deep = "[" * 200000 + "]" * 200000
    (path / "diagnostic_report.json").write_text('{"resourceType": "DiagnosticReport", "id": "x", "extra": %s}' % deep)
    out = dxreport.publish_diagnostic_report(context(), tmp_path, "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out["error"].startswith("RecursionError") and xnat["published"] == []


# ── local review before round 4 ────────────────────────────────────────────────
def test_values_fit_the_columns_xnat_makes_from_the_schema():
    doc = card_report(code={"coding": [{"system": "s" * 300, "code": "c" * 300, "display": "d" * 300}]}, id="r" * 300)
    doc["contained"][2]["code"] = {"text": "h" * 300}
    doc["contained"][2]["valueQuantity"] = {"value": float("nan")}
    doc["contained"][2]["component"] = [{"code": {"text": "Confidence"}, "valueQuantity": {"value": 1e39}},
                                        {"code": {"text": "Instance number"}, "valueInteger": 2 ** 40}]
    values = dxreport.build_values(dxreport.parse(doc), "{}", FACTS, "urn:xnatworks:card:nv-reason-ct", "n")
    assert {len(values[k]) for k in ("code_system", "code", "code_display", "source_id")} == {dxreport.STRING_MAX}
    row = values["findings"][0]
    assert len(row["display"]) == dxreport.STRING_MAX
    assert (row["value_number"], row["confidence"], row["instance_number"]) == (None, None, None), "NaN, over float32, over int32"
    root = ElementTree.fromstring(dxreport.build_record_xml(values).encode())
    assert root.find(DX + "findings")[0].find(DX + "value_number") is None


def test_the_report_file_name_is_a_name_not_a_path(tmp_path, xnat):
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path, card_report(meta={"versionId": "1/../../x"})), "raw",
                                             "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out["file"] == "diagnosticreport-3f2a9c1d0b7e4a65-v1_.._.._x.json", "one name: no separator survives"


def test_an_id_longer_than_the_column_still_finds_its_own_record(tmp_path, xnat):
    long_id = "a" * 300
    xnat["probe"] = 200
    xnat["there"] = {"source_id": "a" * dxreport.STRING_MAX, "source_system": "urn:xnatworks:card:nv-reason-ct"}
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path, card_report(id=long_id)), "raw", "diagnostic_report.json",
                                             "SUCCEEDED", CONTRACT)
    assert out["exists"] is True



# ── Codex round 4 on PR #23, and container-workshop PR #65 (Device is no performer) ──
def hoppr_shaped(**overrides):
    """A report shaped as HOPPR's and the NV-Reason-CT card's (0.6.0): no performer, the model a contained
    Device that every finding names as its ``device``, which is where FHIR R5 puts it."""
    doc = card_report(**overrides)
    if "performer" not in overrides:
        doc.pop("performer")
    for item in doc["contained"]:
        if item["resourceType"] == "Observation":
            item["device"] = {"reference": "#model"}
    return doc


def test_the_device_the_findings_name_is_the_author_without_a_performer():
    parsed = dxreport.parse(hoppr_shaped())
    assert (parsed["performer"], parsed["device_name"], dxreport.report_kind(parsed)) == (None, "NVIDIA NV-Reason-CT", "AI_DRAFT")
    # control: the same document with its findings naming no device and no performer is a human draft
    doc = hoppr_shaped()
    for item in doc["contained"]:
        item.pop("device", None)
    assert dxreport.report_kind(dxreport.parse(doc)) == "HUMAN_DRAFT"
    # a human performer beside the model's findings: the person signs, the model is still named
    signed = dxreport.parse(hoppr_shaped(performer=[{"display": "Dr Example"}], status="final"))
    assert (dxreport.report_kind(signed), signed["device_name"]) == ("FINAL_SIGNED", "NVIDIA NV-Reason-CT")


def test_distinct_cards_never_share_a_label():
    labels = {card: dxreport.record_label("HB0004_1", "urn:xnatworks:card:" + card, "r1")
              for card in ("a-b", "ab", "a.b", "very-long-card-name-one", "very-long-card-name-two")}
    assert len(set(labels.values())) == 5, labels
    assert labels["a-b"] == "HB0004_1_RPT_a-b_r1" and labels["ab"] == "HB0004_1_RPT_ab_r1"
    assert all(len(label) <= dxreport.LABEL_MAX for label in labels.values())


def test_a_create_race_on_the_same_report_is_already_filed_and_on_another_a_collision(tmp_path, xnat):
    xnat["conflict"] = True
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out == {"xsi_type": "dxreport:sessionReportData", "label": "HB0004_1_RPT_nv-reason-ct_3f2a9c1d0b7e4a65", "exists": True,
                   "source_id": "3f2a9c1d0b7e4a65"}
    xnat["there"] = {"source_id": "other", "source_system": "urn:xnatworks:card:nv-reason-ct"}
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out["error"].startswith("label HB0004_1_RPT_nv-reason-ct_3f2a9c1d0b7e4a65 is taken by report 'other'")
    assert xnat["published"] == []


def test_the_report_is_created_without_the_random_suffix_retry(tmp_path, xnat):
    dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert xnat["published"][0]["retry_on_conflict"] is False



# ── Codex round 5 on PR #23 ────────────────────────────────────────────────────
def test_an_xhtml_narrative_is_text_with_its_entities_decoded_once():
    parsed = dxreport.parse(card_report(presentedForm=[], text={"div": "<div><p>Heart &amp; lungs &lt;normal&gt;</p></div>"}))
    assert parsed["narrative"] == "Heart & lungs <normal>"
    values = dxreport.build_values(parsed, "{}", FACTS, "urn:x:card", "n")
    root = ElementTree.fromstring(dxreport.build_record_xml(values).encode())
    assert root.find(DX + "narrative").text == "Heart & lungs <normal>"


def test_a_race_seen_by_the_publishers_own_probe_is_read_like_a_409(tmp_path, xnat):
    xnat["inner_probe_taken"] = True
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out["exists"] is True and out["label"] == "HB0004_1_RPT_nv-reason-ct_3f2a9c1d0b7e4a65"
    xnat["there"] = {"source_id": "other", "source_system": "urn:xnatworks:card:nv-reason-ct"}
    out = dxreport.publish_diagnostic_report(context(), written(tmp_path), "raw", "diagnostic_report.json", "SUCCEEDED", CONTRACT)
    assert out["error"].startswith("label HB0004_1_RPT_nv-reason-ct_3f2a9c1d0b7e4a65 is taken by report 'other'")


def test_the_publishers_taken_label_message_is_the_one_the_race_handler_reads(monkeypatch):
    """The race handler matches publish_record's own words; this pins them so a reworded message cannot
    silently turn an already-filed report into an error."""
    from segwrapup import publish
    monkeypatch.setattr(publish, "_request", lambda ctx, method, url, timeout: 200)
    with pytest.raises(RuntimeError) as raised:
        publish.publish_record(context(), "HB0004_1_RPT_x_y", "<x/>", {}, xsi_type=dxreport.XSI_TYPE, retry_on_conflict=False)
    assert "label HB0004_1_RPT_x_y already exists" in str(raised.value)



# ── Codex round 6 on PR #23 ────────────────────────────────────────────────────
def test_external_results_keep_contained_supporting_observations_out():
    doc = hoppr_shaped(result=[{"reference": "Observation/123"}])
    parsed = dxreport.parse(doc)
    assert parsed["findings"] == [], "the findings are elsewhere; the contained ones only support"
    assert (parsed["device_name"], dxreport.report_kind(parsed)) == (None, "HUMAN_DRAFT"), "no finding names the model"
    # control: with no result element at all, the contained observations are the findings
    del doc["result"]
    assert len(dxreport.parse(doc)["findings"]) == 2


# ── Codex round 7 on PR #23 ────────────────────────────────────────────────────
@pytest.mark.parametrize("performer, contained, expected", [
    ({"reference": "Practitioner/123"}, None, "Practitioner/123"),
    ({"reference": "#org"}, {"resourceType": "Organization", "id": "org", "name": "Radiology Partners"}, "Radiology Partners"),
    ({"reference": "#org"}, {"resourceType": "Organization", "id": "org"}, "#org"),
    ({"reference": "#dr"}, {"resourceType": "Practitioner", "id": "dr", "name": [{"given": ["Ada", "M"], "family": "Lovelace"}]}, "Ada M Lovelace"),
    ({"reference": "#team"}, {"resourceType": "CareTeam", "id": "team", "name": "Night reads"}, "Night reads"),
    ({"reference": "#role"}, {"resourceType": "PractitionerRole", "id": "role", "practitioner": {"display": "Dr Role"}}, "Dr Role"),
    ({"identifier": {"system": "urn:npi", "value": "1234567890"}}, None, "1234567890"),
])
def test_a_performer_without_a_display_still_signs_the_report(performer, contained, expected):
    doc = hoppr_shaped(performer=[performer], status="final")
    if contained:
        doc["contained"].append(contained)
    parsed = dxreport.parse(doc)
    assert parsed["performer"] == expected
    assert dxreport.report_kind(parsed) == "FINAL_SIGNED", "a signed report whose findings name the model is still signed"


@pytest.mark.parametrize("performer", [
    {"reference": "Device/9"}, {"reference": "https://fhir.example/Device/9/_history/2"},
    {"type": "Device", "identifier": {"value": "scanner-1"}}, {"reference": "#model"}, {"reference": "#nobody"}, {}])
def test_a_device_or_a_dangling_performer_names_nobody(performer):
    parsed = dxreport.parse(hoppr_shaped(performer=[performer], status="final"))
    assert parsed["performer"] is None
    assert dxreport.report_kind(parsed) == "AI_DRAFT"


# ── Codex round 8 on PR #23 ────────────────────────────────────────────────────
@pytest.mark.parametrize("result", [
    [{"identifier": {"system": "urn:lab", "value": "obs-123"}}],
    [{"type": "Observation", "display": "measured elsewhere"}],
    ["not a reference"],
])
def test_a_result_that_names_no_contained_observation_still_disables_the_fallback(result):
    parsed = dxreport.parse(hoppr_shaped(result=result))
    assert parsed["findings"] == [], "the findings are elsewhere; the contained ones only support"
    assert (parsed["device_name"], dxreport.report_kind(parsed)) == (None, "HUMAN_DRAFT"), "no finding names the model"


def test_an_identifier_result_beside_a_contained_one_keeps_only_the_contained_one():
    doc = hoppr_shaped(result=[{"identifier": {"value": "obs-123"}}, {"reference": "#finding-2"}])
    parsed = dxreport.parse(doc)
    assert [f["display"] for f in parsed["findings"]] == ["Direct question"]
    assert dxreport.report_kind(parsed) == "AI_DRAFT", "the finding it references names the model"


# ── Codex round 11 on PR #23 ───────────────────────────────────────────────────
def test_wrapped_base64_is_decoded_and_other_junk_is_still_refused(caplog):
    encoded = base64.encodebytes(REPORT_TEXT.encode() * 3).decode()     # wrapped at 76 characters, newline-terminated
    assert "\n" in encoded.strip()
    wrapped = {"contentType": "text/plain", "data": encoded.replace("\n", "\r\n  ")}
    assert dxreport.parse(card_report(presentedForm=[wrapped]))["narrative"] == REPORT_TEXT * 3
    # control: characters outside the base64 alphabet are still refused, and the next attachment is used
    junk = {"contentType": "text/plain", "data": "SGVsbG8@@"}
    plain = {"contentType": "text/plain", "data": base64.b64encode(b"Next.").decode()}
    assert dxreport.parse(card_report(presentedForm=[junk, plain]))["narrative"] == "Next."
    assert "not base64" in caplog.text

