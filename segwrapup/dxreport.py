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
import hashlib
import html
import http.client
import json
import logging
import math
import re
import urllib.error
import urllib.parse
from pathlib import Path, PurePosixPath
from xml.sax.saxutils import escape

from .execution import _get_json
from .publish import RecordFile, _request, _xml_text, publish_record, record_urls
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
#: A plain xs:string element is a varchar(255) column in XNAT: one value over it fails the whole create.
STRING_MAX = 255
#: xs:float is a single-precision column and xs:int a 32-bit one: a value outside them fails the create too.
FLOAT_MAX = 3.4028234663852886e38
INT_RANGE = (-2 ** 31, 2 ** 31 - 1)
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
    """A JSON number as a float; None for anything else, and for an integer too large for one (JSON
    allows thousands of digits, ``float()`` raises OverflowError on them: Codex P1, PR #23)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        logger.warning("a number of %d digits is too large for the record; left off", len(str(value)))
        return None
    # json.loads accepts NaN and Infinity, and xs:float is single precision: neither fits the column
    if not math.isfinite(number) or abs(number) > FLOAT_MAX:
        logger.warning("the number %r does not fit the record's single-precision column; left off", number)
        return None
    return number


#: A Device named by URL rather than contained: ``Device/9``, ``https://fhir.example/Device/9/_history/2``.
DEVICE_URL = re.compile(r"(^|/)Device/[^/]+(/_history/[^/]+)?$")


def _performer_name(resource: dict) -> str | None:
    """A contained performer's name: a Practitioner's first HumanName (its text, else its given and
    family names), an Organization's or CareTeam's name, or the practitioner a PractitionerRole displays."""
    name = resource.get("name")
    if isinstance(name, list):
        human = _first(name)
        given = human.get("given") if isinstance(human.get("given"), list) else []
        return _text(human.get("text")) or _text(" ".join(filter(None, (_text(part) for part in given + [human.get("family")]))))
    if name is not None:
        return _text(name)
    practitioner = resource.get("practitioner")
    return _text(practitioner.get("display")) if isinstance(practitioner, dict) else None


def performer(doc: dict) -> str | None:
    """Who performed the report: the first performer's name, else the reference that identifies them.
    A performer is present whether or not it is named: FHIR leaves ``Reference.display`` optional, and a
    signed report referencing ``Practitioner/123`` is still signed (Codex P2, PR #23). A performer that
    is a Device, contained or by URL, is a model, not a person, whatever its display says (FHIR R5 does
    not allow one there, but a document may still carry it), and a ``#`` reference to nothing contained
    names nobody."""
    for item in doc.get("performer") or []:
        if not isinstance(item, dict):
            continue
        reference = _text(item.get("reference"))
        target = _contained(doc, reference)
        if _text(item.get("type")) == "Device" or (target is not None and target.get("resourceType") == "Device") \
                or (reference is not None and DEVICE_URL.search(reference)):
            continue
        if reference is not None and reference.startswith("#") and target is None:
            logger.warning("performer %s references nothing the report contains; it names nobody", reference)
            continue
        identifier = item.get("identifier")
        name = _text(item.get("display")) or (_performer_name(target) if target is not None else None)
        found = name or reference or (_text(identifier.get("value")) if isinstance(identifier, dict) else None)
        if found:
            return found
    return None


def author_device(doc: dict) -> dict | None:
    """The contained Device that wrote the report: the one the findings name as their ``device``,
    which is where FHIR R5 puts a model (a report's ``performer`` takes only people and
    organisations; HOPPR's report and the NV-Reason-CT card's are shaped so), else, tolerated, one a
    performer entry references. A contained Device nothing points at (the scanner, say) says
    nothing about who wrote the report (Codex P1, PR #23: the first contained Device made a human's
    draft with a scanner in it an AI draft; Codex P2, container-workshop PR #65: Device is not a
    permitted performer)."""
    for observation in _observations(doc):
        device = observation.get("device")
        target = _contained(doc, device.get("reference")) if isinstance(device, dict) else None
        if target is not None and target.get("resourceType") == "Device":
            return target
    for item in doc.get("performer") or []:
        target = _contained(doc, item.get("reference")) if isinstance(item, dict) else None
        if target is not None and target.get("resourceType") == "Device":
            return target
    return None


def _device(device: dict | None, array: str) -> str | None:
    return _text(_first(device.get(array)).get("value")) if device else None


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
    """The first ``presentedForm`` attachment that is text, decoded; else the resource's own XHTML text,
    untagged. ``presentedForm`` repeats and may hold any type (FHIR R5 suggests a PDF): an attachment
    that is not text, or not base64 UTF-8, is passed over for the next one and the XHTML, not taken
    as the end of the narrative (Codex P2, PR #23)."""
    for index, form in enumerate(doc.get("presentedForm") or []):
        data = _text((form or {}).get("data"))
        if not data:
            continue
        media, _, parameters = (_text((form or {}).get("contentType")) or "").partition(";")
        media = media.strip().lower()
        if media and not media.startswith("text/"):
            logger.info("presentedForm %d is %s, not text; looking further for the narrative", index, media)
            continue
        charset = re.search(r"charset\s*=\s*\"?([A-Za-z0-9._-]+)", parameters, re.I)
        encoding = charset.group(1) if charset else "utf-8"
        try:
            return base64.b64decode(data, validate=True).decode(encoding)
        except (binascii.Error, ValueError, LookupError) as error:
            # LookupError: a charset Python does not know
            logger.warning("presentedForm %d is not base64 %s text (%s); looking further for the narrative", index, encoding, error)
    div = _text((doc.get("text") or {}).get("div")) if isinstance(doc.get("text"), dict) else None
    # tags out, then entities decoded: "Heart &amp; lungs" is "Heart & lungs", and the record XML escapes it
    # once itself (Codex P2, PR #23: it read "&amp;amp;")
    return html.unescape(re.sub(r"<[^>]+>", " ", div)).strip() if div else None


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
            value = component.get("valueInteger")
            instance_number = value if isinstance(value, int) and not isinstance(value, bool) \
                and INT_RANGE[0] <= value <= INT_RANGE[1] else None
        elif value_number is None and component_quantity:
            value_number, unit = _number(component_quantity.get("value")), _text(component_quantity.get("unit"))
    return {"code_system": _text(coding.get("system")), "code": _text(coding.get("code")), "display": display,
            "text": code_text if code_text and code_text != display else None, "value_string": value_string,
            "value_number": value_number, "unit": unit, "confidence": confidence,
            "body_site": _coding(observation.get("bodySite"), "display"), "instance_number": instance_number}


def _observations(doc: dict) -> list[dict]:
    """The contained Observations the report's ``result`` references; every contained Observation only when
    the report has no ``result`` at all. A report whose results are external, by reference
    (``Observation/123``) or by ``identifier`` alone, keeps none: a contained supporting Observation is
    not one of its findings, and must not make it an AI draft through its ``device`` (Codex P2, PR #23,
    rounds 6 and 8)."""
    results = doc.get("result")
    if isinstance(results, list) and results:
        references = (r.get("reference") for r in results if isinstance(r, dict))
        return [o for o in (_contained(doc, r) for r in references) if o is not None and o.get("resourceType") == "Observation"]
    return [o for o in doc.get("contained") or [] if isinstance(o, dict) and o.get("resourceType") == "Observation"]


def findings(doc: dict) -> list[dict]:
    return [finding(o) for o in _observations(doc)]


def parse(doc) -> dict:
    """The fields of a FHIR R5 DiagnosticReport the record carries. Raises NotADiagnosticReport."""
    if not isinstance(doc, dict) or doc.get("resourceType") != "DiagnosticReport":
        raise NotADiagnosticReport("expected a DiagnosticReport, got %r" % (doc.get("resourceType") if isinstance(doc, dict) else type(doc).__name__))
    report_id = _text(doc.get("id"))
    if not report_id:
        raise NotADiagnosticReport("the DiagnosticReport has no id: a rerun could not find the record it already made")
    tags = (doc.get("meta") or {}).get("tag") if isinstance(doc.get("meta"), dict) else None
    device = author_device(doc)
    return {"id": report_id, "version_id": _text((doc.get("meta") or {}).get("versionId")) if isinstance(doc.get("meta"), dict) else None,
            "status": _text(doc.get("status")), "category": _coding(_first(doc.get("category")), "code"),
            "code_system": _coding(doc.get("code"), "system"), "code": _coding(doc.get("code"), "code"),
            "code_display": _coding(doc.get("code"), "display"), "issued": _text(doc.get("issued")),
            "effective": _text(doc.get("effectiveDateTime")), "performer": performer(doc),
            "device_author": device is not None,
            "device_name": _device(device, "name"), "device_version": _device(device, "version"),
            "simulated": any(isinstance(t, dict) and str(t.get("code", "")).lower() == "simulated" for t in tags or []),
            "conclusion": _text(doc.get("conclusion")), "narrative": narrative(doc),
            "study_instance_uid": study_instance_uid(doc), "findings": findings(doc)}


def report_kind(parsed: dict) -> str:
    """The poller's rule: a Device author and no human performer is an AI draft; a human performer
    signs a final report; anything else is a human draft. The Device is the one the findings or a
    performer point at (``author_device``), not any contained Device."""
    # a referenced Device is the author whether or not it carries a name, which FHIR leaves optional (Codex P2, PR #23)
    if parsed["performer"] is None and parsed.get("device_author", parsed["device_name"] is not None):
        return "AI_DRAFT"
    if parsed["performer"] is not None and (parsed["status"] or "").lower() == "final":
        return "FINAL_SIGNED"
    return "HUMAN_DRAFT"


SOURCE_PART_MAX = 16
#: The longest end the label keeps whole: ``_RPT_`` + a card id of 16 + ``_`` + a 16-hex report id is 38,
#: and at least 24 characters are left for the session label.
SUFFIX_MAX = 40


def _tail(source: str) -> str:
    """The last path segment of a URL or URN: ``urn:xnatworks:card:nv-reason-ct`` -> ``nv-reason-ct``."""
    s = re.sub(r"[/:]+$", "", source or "")
    cut = max(s.rfind("/"), s.rfind(":"))
    return (s[cut + 1:] if 0 <= cut < len(s) - 1 else s) or "src"


def _source_part(source_system: str) -> str:
    """The card in the label: its id as it is when the label can carry it and it is short, else
    shortened with a hash of the whole source. The poller keeps the first 12 alphanumerics, which made
    cards ``a-b`` and ``ab`` (or two sharing 12 characters) one label, and the second card's report a
    collision it could never file (Codex P2, PR #23). The tail is unique among card sources
    (``urn:xnatworks:card:<card id>``), which is all this module files."""
    part = _spelled(_tail(source_system), 6)
    if len(part) > SOURCE_PART_MAX:
        part = _label_part(_tail(source_system))[:SOURCE_PART_MAX - 7].strip("_") + "_" + _short_hash(source_system, 6)
    return part


def _label_part(text: str) -> str:
    """``text`` in the characters an XNAT label takes (letters, digits, ``_`` and ``-``), runs of ``_`` as one."""
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_-]+", "_", text or ""))


def _spelled(text: str, width: int = 8) -> str:
    """``text`` as a label part that stays distinct: as it is when the label can carry it unchanged,
    else its rewritten form with a hash of the original, since ``SUB.01`` and ``SUB_01`` both
    rewrite to ``SUB_01`` (Codex P1/P2, PR #23)."""
    clean = _label_part(text)
    return clean if clean == (text or "") else f"{clean.strip('_')}_{_short_hash(text, width)}".lstrip("_")


def _short_hash(text: str, width: int) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "surrogatepass")).hexdigest()[:width]


def record_label(session_label: str, source_system: str, report_id: str) -> str:
    """``<session label>_RPT_<source>_<report id>``, as the poller names its records (DxReportRecordMapper.label).

    Within the 64-character cap the end of the label, which names the report, is kept whole and the
    session label is shortened instead, with a hash of the whole of it so two long session labels
    that share a beginning stay apart. A report id too long for the suffix is replaced by its hash.
    Cutting the whole label from the right made every report on a session with a 60-character label
    ``<label>_RPT``, so the existence probe took each new report for one already filed (Codex P1, PR #23)."""
    # the id and the session label as the label can carry them unchanged, else rewritten with a hash of
    # the original beside them: "a-b"/"a.b" and "SUB.01"/"SUB_01" rewrite alike, and two reports or two
    # sessions must never share a label (Codex P1 and P2, PR #23)
    source = _source_part(source_system)
    suffix = f"_RPT_{source}_{_spelled(report_id)}"
    if len(suffix) > SUFFIX_MAX:
        suffix = f"_RPT_{source}_{_short_hash(report_id, 16)}"
    session = _spelled(session_label, 6).rstrip("_")
    room = LABEL_MAX - len(suffix)
    if len(session) > room:
        session = _label_part(session_label)[:room - 7].rstrip("_") + "_" + _short_hash(session_label, 6)
    return session + suffix


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


def _xml_safe(value, what: str):
    """Strings with every code point XML 1.0 forbids written as its escape (``publish._xml_text``):
    ``json.loads`` accepts ``"\\u0001"``, and one such character made the whole create document
    malformed (Codex P2, PR #23). Done before the caps, so a cap counts what is written."""
    if isinstance(value, str):
        return _xml_text(what, value)
    if isinstance(value, dict):
        return {k: _xml_safe(v, f"{what} {k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_xml_safe(v, what) for v in value]
    return value


def build_values(parsed: dict, raw_json: str, facts: dict, source_system: str, pseudonymization: str) -> dict:
    """Everything the record holds. ``facts`` is the session as XNAT has it (``fetch_session_facts``):
    the record names the archived session and its subject, never what the document says about them."""
    parsed, raw_json, facts = _xml_safe(parsed, "report"), _xml_safe(raw_json, "raw_json"), _xml_safe(facts, "session")
    category = (parsed["category"] or "RAD").upper()
    status = (parsed["status"] or "unknown").lower()
    if raw_json is not None and len(raw_json) > RAW_JSON_MAX:
        logger.warning("the DiagnosticReport is %d characters, over the record's raw_json cap of %d; the record leaves raw_json "
                       "empty and the document stays whole in the REPORT resource", len(raw_json), RAW_JSON_MAX)
        raw_json = None
    rows = [{k: (_clip(v, FINDING_TEXT_MAX, f"finding {k}") if k in ("text", "value_string")
                 else _clip(v, STRING_MAX, f"finding {k}") if isinstance(v, str) else v) for k, v in row.items()}
            for row in parsed["findings"]]
    short = {k: _clip(parsed[k], STRING_MAX, k) for k in ("code_system", "code", "code_display", "version_id", "performer",
                                                           "device_name", "device_version", "id")}
    return {"project": facts["project"], "session_id": facts["id"],
            "label": record_label(facts["label"], source_system, parsed["id"]),
            "category": category if category in CATEGORIES else "OTH", "code_system": short["code_system"], "code": short["code"],
            "code_display": short["code_display"], "report_status": status if status in STATUSES else "unknown",
            "report_kind": report_kind(parsed), "source_system": _clip(source_system, STRING_MAX, "source_system"),
            "source_format": SOURCE_FORMAT, "source_id": short["id"], "source_version": short["version_id"] or "1",
            "issued": _when(parsed["issued"], "issued"), "effective": _when(parsed["effective"], "effectiveDateTime"),
            "performer": short["performer"], "device_name": short["device_name"], "device_version": short["device_version"],
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


def _existing(context: XnatContext, label_url: str, label: str, values: dict, timeout_seconds: float, race: bool = False) -> dict:
    """The outcome when the label is already taken. The report id is the card's, deterministic for the
    same inputs, so the same report is already filed, but only if the record there says so: a label
    is a lossy spelling of the identity, and a different report under it is a collision to report, not
    a report to drop (Codex P1, PR #23). Compared as the record holds them (cut to the column). Raises
    what the read raises."""
    there = _get_json(context, f"{label_url}?format=json", min(timeout_seconds, 60.0))["items"][0]["data_fields"]
    if (there.get("source_id"), there.get("source_system")) != (values["source_id"], values["source_system"]):
        logger.error("label %s on session %s holds report %r from %r, not %r from %r; not filed", label, context.session,
                     there.get("source_id"), there.get("source_system"), values["source_id"], values["source_system"])
        return {"error": f"label {label} is taken by report {there.get('source_id')!r} from {there.get('source_system')!r}"}
    logger.info("diagnostic report %s already on session %s%s; not filed twice", label, context.session,
                " (filed by a concurrent run)" if race else "")
    return {"xsi_type": XSI_TYPE, "label": label, "exists": True, "source_id": values["source_id"]}


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
    except (OSError, UnicodeDecodeError, ValueError, AttributeError, TypeError, KeyError, IndexError, ArithmeticError,
            RecursionError) as error:
        # RecursionError: json.loads on a valid but extremely deeply nested document (Codex P2, PR #23)
        # NotADiagnosticReport and JSONDecodeError are ValueErrors; a valid JSON document with a malformed
        # nested value (presentedForm ["oops"], a string where a CodeableConcept goes) fails the parser's
        # .get() with AttributeError or TypeError, and must not escape after the run record exists (Codex P1, PR #23)
        logger.error("diagnostic report %s not filed: %s: %s", declared, type(error).__name__, error)
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
            return _existing(context, label_url, label, values, timeout_seconds)
        xml = build_record_xml(values)
        # both parts sanitised: the name is a path on the record, so a versionId of "1/../x" must not make it one
        name = "diagnosticreport-%s-v%s.json" % tuple(re.sub(r"[^A-Za-z0-9_.-]+", "_", part).strip(".") or "_"
                                                      for part in (parsed["id"], values["source_version"]))
        try:
            outcome = publish_record(context, label, xml, {REPORT_ROLE: [RecordFile(path, name)]}, timeout_seconds=timeout_seconds,
                                     xsi_type=XSI_TYPE, retry_on_conflict=False)
        except RuntimeError as error:
            # another wrapup filed a report under this label after the probe above: publish_record's own
            # probe sees it ("already exists") or its create gets a 409. Either way the record there is
            # read: the same report (a concurrent rerun) is already filed, anything else is a collision.
            # Never a second record under a random label (Codex P2 x2, PR #23)
            if "HTTP 409" not in str(error) and f"label {label} already exists" not in str(error):
                raise
            return _existing(context, label_url, label, values, timeout_seconds, race=True)
    except (RuntimeError, urllib.error.URLError, http.client.HTTPException, OSError, KeyError, IndexError, TypeError, ValueError,
            AttributeError, ArithmeticError) as error:
        # http.client.HTTPException: a truncated XNAT answer raises IncompleteRead, which is not an OSError (Codex P1, PR #23)
        logger.error("diagnostic report %s not filed on session %s; the run record stays: %s: %s", declared, context.session,
                     type(error).__name__, error)
        return {"error": f"{type(error).__name__}: {error}"}
    logger.info("diagnostic report filed as %s %s (%s, %d findings)", outcome.get("id"), label, values["report_kind"], len(values["findings"]))
    return {"xsi_type": XSI_TYPE, "id": outcome.get("id"), "label": outcome.get("label") or label, "source_id": parsed["id"],
            "report_kind": values["report_kind"], "findings": len(values["findings"]), "file": name}
