"""Diagnostic report records (proc-wrapup 0.7.2).

A card whose tool writes a FHIR R5 ``DiagnosticReport`` declares it in its results contract
(``XNW_DIAGNOSTIC_REPORT``: the file's path inside the tool's output). After the run record,
proc-wrapup files the report as a ``dxreport:sessionReportData`` on the session: the datatype
of ``xnat-dxreport-schema-plugin``, the one the FHIR report poller writes for reports that come
from a FHIR server (HOPPR's on the RSNA demo). The mapping follows that poller's
``DiagnosticReportParser`` and ``DxReportRecordMapper``, so a report a card wrote and a report a
vendor sent read the same on the session page.

The card does the model-specific part (turning its tool's text into a DiagnosticReport); this
module only reads FHIR and talks to XNAT. It never raises: the run record is already published
and stays, and every failure is an outcome in ``wrapup.json``.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import urllib.error
import urllib.parse
from pathlib import Path, PurePosixPath
from xml.sax.saxutils import escape

from .execution import _get_json
from .publish import RecordFile, _request, publish_record, record_urls
from .register import XnatContext

logger = logging.getLogger(__name__)

XSI_TYPE = "dxreport:sessionReportData"
DXREPORT_NS = "http://xnatworks.io/dxreport"
XNAT_NS = "http://nrg.wustl.edu/xnat"
DICOM_UID_SYSTEM = "urn:dicom:uid"
OID_PREFIX = "urn:oid:"
SOURCE_FORMAT = "FHIR_R5"
REPORT_ROLE = "REPORT"
#: The poller's label cap (DxReportRecordMapper.MAX_LABEL).
LABEL_MAX = 64

#: The schema's enumerations (dxreport.xsd): a value outside them would fail the whole create.
CATEGORIES = {"RAD", "PAT", "LAB", "CG", "CT", "NRS", "OTH"}
STATUSES = {"registered", "partial", "preliminary", "final", "amended", "corrected", "appended", "cancelled",
            "entered-in-error", "unknown"}
#: The schema's text caps (text4000, text8000, text100000, text200000).
FINDING_TEXT_MAX = 4000
CONCLUSION_MAX = 8000
NARRATIVE_MAX = 100000
RAW_JSON_MAX = 200000
#: xs:dateTime as FHIR writes an instant or dateTime with a time part; a date alone is not one.
DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$")


class NotADiagnosticReport(ValueError):
    """The declared file is not a FHIR DiagnosticReport this module can file."""


def _text(value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _first(items) -> dict:
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}


def _coding(codeable, field: str) -> str | None:
    """A field of the first coding; for ``display``, the CodeableConcept's ``text`` when no coding has one."""
    if not isinstance(codeable, dict):
        return None
    value = _text(_first(codeable.get("coding")).get(field))
    return value if value is not None or field != "display" else _text(codeable.get("text"))


def _contained(doc: dict, reference) -> dict | None:
    if not isinstance(reference, str) or not reference.startswith("#"):
        return None
    for item in doc.get("contained") or []:
        if isinstance(item, dict) and item.get("id") == reference[1:]:
            return item
    return None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def performer(doc: dict) -> str | None:
    """The human performer's name. A performer that resolves to a contained Device is the model, not
    a person, whatever its display says: it makes the report an AI draft, not a human one."""
    for item in doc.get("performer") or []:
        if not isinstance(item, dict):
            continue
        target = _contained(doc, item.get("reference"))
        if target is not None and target.get("resourceType") == "Device":
            continue
        display = _text(item.get("display"))
        if display:
            return display
        if target is not None and target.get("resourceType") == "Practitioner":
            return _text(_first(target.get("name")).get("text"))
    return None


def _device(doc: dict, array: str) -> str | None:
    for item in doc.get("contained") or []:
        if isinstance(item, dict) and item.get("resourceType") == "Device":
            return _text(_first(item.get(array)).get("value"))
    return None


def study_instance_uid(doc: dict) -> str | None:
    for item in doc.get("contained") or []:
        if not isinstance(item, dict) or item.get("resourceType") != "ImagingStudy":
            continue
        for identifier in item.get("identifier") or []:
            value = _text((identifier or {}).get("value"))
            if value and ((identifier or {}).get("system") == DICOM_UID_SYSTEM or value.startswith(OID_PREFIX)):
                return value[len(OID_PREFIX):] if value.startswith(OID_PREFIX) else value
    return None


def narrative(doc: dict) -> str | None:
    """The first base64 ``presentedForm`` attachment, decoded; else the resource's own XHTML text, untagged."""
    for form in doc.get("presentedForm") or []:
        data = _text((form or {}).get("data"))
        if data:
            try:
                return base64.b64decode(data, validate=True).decode("utf-8")
            except (binascii.Error, ValueError) as error:
                logger.warning("presentedForm is not base64 UTF-8 text (%s); the record keeps no narrative from it", error)
                return None
    div = _text((doc.get("text") or {}).get("div")) if isinstance(doc.get("text"), dict) else None
    return re.sub(r"<[^>]+>", " ", div).strip() if div else None


def finding(observation: dict) -> dict:
    coding = _first((observation.get("code") or {}).get("coding"))
    code_text = _text((observation.get("code") or {}).get("text"))
    display = _text(coding.get("display")) or code_text
    value_string = _text(observation.get("valueString")) or _text(_first(observation.get("note")).get("text"))
    quantity = observation.get("valueQuantity") if isinstance(observation.get("valueQuantity"), dict) else {}
    value_number, unit = _number(quantity.get("value")), _text(quantity.get("unit"))
    confidence = instance_number = None
    for component in observation.get("component") or []:
        if not isinstance(component, dict):
            continue
        label = (_text((component.get("code") or {}).get("text")) or _coding(component.get("code"), "display") or "").lower()
        component_quantity = component.get("valueQuantity") if isinstance(component.get("valueQuantity"), dict) else {}
        if "confidence" in label:
            confidence = _number(component_quantity.get("value"))
        elif "instance number" in label:
            instance_number = component.get("valueInteger") if isinstance(component.get("valueInteger"), int) else None
        elif value_number is None and component_quantity:
            value_number, unit = _number(component_quantity.get("value")), _text(component_quantity.get("unit"))
    return {"code_system": _text(coding.get("system")), "code": _text(coding.get("code")), "display": display,
            "text": code_text if code_text and code_text != display else None, "value_string": value_string,
            "value_number": value_number, "unit": unit, "confidence": confidence,
            "body_site": _coding(observation.get("bodySite"), "display"), "instance_number": instance_number}


def findings(doc: dict) -> list[dict]:
    """The Observations the report's ``result`` references; every contained Observation when it references none."""
    observations = [o for o in (_contained(doc, (r or {}).get("reference")) for r in doc.get("result") or [])
                    if o is not None and o.get("resourceType") == "Observation"]
    if not observations:
        observations = [o for o in doc.get("contained") or [] if isinstance(o, dict) and o.get("resourceType") == "Observation"]
    return [finding(o) for o in observations]


def parse(doc) -> dict:
    """The fields of a FHIR R5 DiagnosticReport the record carries. Raises NotADiagnosticReport."""
    if not isinstance(doc, dict) or doc.get("resourceType") != "DiagnosticReport":
        raise NotADiagnosticReport("expected a DiagnosticReport, got %r" % (doc.get("resourceType") if isinstance(doc, dict) else type(doc).__name__))
    report_id = _text(doc.get("id"))
    if not report_id:
        raise NotADiagnosticReport("the DiagnosticReport has no id: a rerun could not find the record it already made")
    tags = (doc.get("meta") or {}).get("tag") if isinstance(doc.get("meta"), dict) else None
    return {"id": report_id, "version_id": _text((doc.get("meta") or {}).get("versionId")) if isinstance(doc.get("meta"), dict) else None,
            "status": _text(doc.get("status")), "category": _coding(_first(doc.get("category")), "code"),
            "code_system": _coding(doc.get("code"), "system"), "code": _coding(doc.get("code"), "code"),
            "code_display": _coding(doc.get("code"), "display"), "issued": _text(doc.get("issued")),
            "effective": _text(doc.get("effectiveDateTime")), "performer": performer(doc),
            "device_name": _device(doc, "name"), "device_version": _device(doc, "version"),
            "simulated": any(isinstance(t, dict) and str(t.get("code", "")).lower() == "simulated" for t in tags or []),
            "conclusion": _text(doc.get("conclusion")), "narrative": narrative(doc),
            "study_instance_uid": study_instance_uid(doc), "findings": findings(doc)}


def report_kind(parsed: dict) -> str:
    """The poller's rule: a Device author and no human performer is an AI draft; a human performer
    signs a final report; anything else is a human draft."""
    if parsed["performer"] is None and parsed["device_name"] is not None:
        return "AI_DRAFT"
    if parsed["performer"] is not None and (parsed["status"] or "").lower() == "final":
        return "FINAL_SIGNED"
    return "HUMAN_DRAFT"


def _tail(source: str, width: int = 12) -> str:
    """The last path segment of a URL or URN, alphanumerics only: ``urn:xnatworks:card:nv-reason-ct`` -> ``nvreasonct``."""
    s = re.sub(r"[/:]+$", "", source or "")
    cut = max(s.rfind("/"), s.rfind(":"))
    if 0 <= cut < len(s) - 1:
        s = s[cut + 1:]
    s = re.sub(r"[^A-Za-z0-9]+", "", s)
    return s[:width] or "src"


def record_label(session_label: str, source_system: str, report_id: str) -> str:
    """``<session label>_RPT_<source>_<report id>``, as the poller names its records (DxReportRecordMapper.label)."""
    raw = f"{session_label}_RPT_{_tail(source_system)}_{report_id}"
    clean = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]+", "_", raw))
    return clean[:LABEL_MAX]


def _clip(value: str | None, limit: int, what: str) -> str | None:
    if value is not None and len(value) > limit:
        logger.warning("%s is %d characters; the record keeps the first %d (the full text is in the REPORT file)", what, len(value), limit)
        return value[:limit]
    return value


def _when(value: str | None, what: str) -> str | None:
    if value and not DATETIME.match(value):
        logger.warning("%s %r is not a date-time the record can hold; left off", what, value)
        return None
    return value


def build_values(parsed: dict, raw_json: str, facts: dict, source_system: str, pseudonymization: str) -> dict:
    """Everything the record holds. ``facts`` is the session as XNAT has it (``fetch_session_facts``):
    the record names the archived session and its subject, never what the document says about them."""
    category = (parsed["category"] or "RAD").upper()
    status = (parsed["status"] or "unknown").lower()
    if raw_json is not None and len(raw_json) > RAW_JSON_MAX:
        logger.warning("the DiagnosticReport is %d characters, over the record's raw_json cap of %d; the record leaves raw_json "
                       "empty and the document stays whole in the REPORT resource", len(raw_json), RAW_JSON_MAX)
        raw_json = None
    rows = [{k: (_clip(v, FINDING_TEXT_MAX, f"finding {k}") if k in ("text", "value_string") else v) for k, v in row.items()}
            for row in parsed["findings"]]
    return {"project": facts["project"], "session_id": facts["id"],
            "label": record_label(facts["label"], source_system, parsed["id"]),
            "category": category if category in CATEGORIES else "OTH", "code_system": parsed["code_system"], "code": parsed["code"],
            "code_display": parsed["code_display"], "report_status": status if status in STATUSES else "unknown",
            "report_kind": report_kind(parsed), "source_system": source_system, "source_format": SOURCE_FORMAT,
            "source_id": parsed["id"], "source_version": parsed["version_id"] or "1",
            "issued": _when(parsed["issued"], "issued"), "effective": _when(parsed["effective"], "effectiveDateTime"),
            "performer": parsed["performer"], "device_name": parsed["device_name"], "device_version": parsed["device_version"],
            "simulated": parsed["simulated"], "pseudonym": facts.get("subject_label"),
            "conclusion": _clip(parsed["conclusion"], CONCLUSION_MAX, "conclusion"),
            "narrative": _clip(parsed["narrative"], NARRATIVE_MAX, "narrative"), "findings": rows,
            "pseudonymization": pseudonymization, "raw_json": raw_json, "modality": facts.get("modality"),
            # the archive's UID, as the poller records it: the document's own is what it claims to describe
            "study_instance_uid": facts.get("uid") or parsed["study_instance_uid"]}


def _element(name: str, value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, float):
        value = repr(value)
    return f"<dxreport:{name}>{escape(str(value))}</dxreport:{name}>"


FINDING_FIELDS = ("code_system", "code", "display", "text", "value_string", "value_number", "unit", "confidence", "body_site",
                  "instance_number")


def build_record_xml(values: dict) -> str:
    """The record as XNAT XML, elements in the XSD's sequence order."""
    rows = "".join("<dxreport:finding>" + "".join(_element(f, row.get(f)) for f in FINDING_FIELDS) + "</dxreport:finding>"
                   for row in values["findings"])
    head = ("category", "code_system", "code", "code_display", "report_status", "report_kind", "source_system", "source_format",
            "source_id", "source_version", "issued", "effective", "performer", "device_name", "device_version", "simulated",
            "pseudonym", "conclusion", "narrative")
    body = ("".join(_element(n, values.get(n)) for n in head)
            + _element("finding_count", len(values["findings"]))
            + (f"<dxreport:findings>{rows}</dxreport:findings>" if rows else "")
            + "".join(_element(n, values.get(n)) for n in ("pseudonymization", "raw_json", "modality", "study_instance_uid")))
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<dxreport:SessionReport xmlns:dxreport="{DXREPORT_NS}" xmlns:xnat="{XNAT_NS}" '
            f'project="{escape(values["project"], {chr(34): "&quot;"})}" label="{escape(values["label"], {chr(34): "&quot;"})}">'
            f'<xnat:imageSession_ID>{escape(values["session_id"])}</xnat:imageSession_ID>{body}</dxreport:SessionReport>')


def fetch_session_facts(context: XnatContext, timeout_seconds: float = 60.0) -> dict:
    """The session's id, label, project, UID and modality, and its subject's label (the record's pseudonym). Raises."""
    fields = _get_json(context, f"{context.host}/data/experiments/{urllib.parse.quote(context.session, safe='')}?format=json",
                       timeout_seconds)["items"][0]["data_fields"]
    subject = _get_json(context, f"{context.host}/data/subjects/{urllib.parse.quote(str(fields['subject_ID']), safe='')}?format=json",
                        timeout_seconds)["items"][0]["data_fields"]
    return {"id": fields["ID"], "label": fields.get("label") or fields["ID"], "project": fields["project"],
            "uid": fields.get("UID"), "modality": fields.get("modality"), "subject_label": subject.get("label")}


def declared_path(output_dir: Path, derived_root: str | None, declared: str) -> Path:
    """The declared file inside the tool's tree. Raises ValueError for a path that would leave it."""
    relative = PurePosixPath(declared.strip())
    if not declared.strip() or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"XNW_DIAGNOSTIC_REPORT={declared!r} must be a path inside the tool's output")
    root = output_dir / derived_root if derived_root else output_dir
    return root.joinpath(*relative.parts)


def publish_diagnostic_report(context: XnatContext | None, output_dir: Path, derived_root: str | None, declared: str,
                              run_status: str, contract: dict, timeout_seconds: float = 300.0) -> dict:
    """File the tool's DiagnosticReport as a ``dxreport:sessionReportData`` on the session. Never raises."""
    if context is None:
        return {"skipped": "no XNAT context"}
    if context.scope != "session":
        return {"skipped": f"a diagnostic report record hangs from a session (this run is {context.scope}-scoped)"}
    if run_status != "SUCCEEDED":
        logger.warning("run is %s; no diagnostic report is filed from a run that did not succeed", run_status)
        return {"skipped": f"run {run_status}"}
    try:
        path = declared_path(output_dir, derived_root, declared)
        if not path.is_file():
            logger.warning("the card declares a diagnostic report at %s but the tool wrote none; no record", declared)
            return {"skipped": f"no {declared} in the tool's output"}
        text = path.read_text(encoding="utf-8")
        parsed = parse(json.loads(text))
    except (OSError, UnicodeDecodeError, ValueError) as error:       # NotADiagnosticReport and JSONDecodeError are ValueErrors
        logger.error("diagnostic report %s not filed: %s", declared, error)
        return {"error": f"{type(error).__name__}: {error}"}
    card_id = contract.get("card_id") or ""
    source_system = f"urn:xnatworks:card:{card_id}" if card_id else "urn:xnatworks:card"
    try:
        facts = fetch_session_facts(context, min(timeout_seconds, 60.0))
        values = build_values(parsed, text, facts, source_system,
                              pseudonymization=(f"None needed: card {card_id} {contract.get('card_revision') or ''} wrote this report "
                                                "inside XNAT from the archived session; no outside document was involved").replace("  ", " "))
        label = values["label"]
        label_url, _ = record_urls(context, label)
        probe = _request(context, "GET", f"{label_url}?format=json", min(timeout_seconds, 60.0))
        if probe == 200:
            # the report id is the card's, deterministic for the same inputs: the same report is already filed
            logger.info("diagnostic report %s already on session %s; not filed twice", label, context.session)
            return {"xsi_type": XSI_TYPE, "label": label, "exists": True, "source_id": parsed["id"]}
        xml = build_record_xml(values)
        name = f"diagnosticreport-{re.sub(r'[^A-Za-z0-9_.-]+', '_', parsed['id'])}-v{values['source_version']}.json"
        outcome = publish_record(context, label, xml, {REPORT_ROLE: [RecordFile(path, name)]}, timeout_seconds=timeout_seconds,
                                 xsi_type=XSI_TYPE)
    except (RuntimeError, urllib.error.URLError, OSError, KeyError, IndexError, TypeError, ValueError) as error:
        logger.error("diagnostic report %s not filed on session %s; the run record stays: %s", declared, context.session, error)
        return {"error": f"{type(error).__name__}: {error}"}
    logger.info("diagnostic report filed as %s %s (%s, %d findings)", outcome.get("id"), label, values["report_kind"], len(values["findings"]))
    return {"xsi_type": XSI_TYPE, "id": outcome.get("id"), "label": outcome.get("label") or label, "source_id": parsed["id"],
            "report_kind": values["report_kind"], "findings": len(values["findings"]), "file": name}
