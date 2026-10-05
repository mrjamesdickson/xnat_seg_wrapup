# Diagnostic report records (proc-wrapup 0.7.2)

A card whose tool writes a FHIR R5 `DiagnosticReport` can have it filed on the session as a
`dxreport:sessionReportData`. James asked for it on 2026-10-05, when NV-Reason-CT 0.5.0 turned
out to write report text and no report record ("it would be a great demo piece").
User-facing description: README, "Diagnostic reports". Code: `segwrapup/dxreport.py`.

## Why this datatype, and why proc-wrapup files it

- **The datatype already exists and already has a reader.** `xnat-dxreport-schema-plugin`
  (namespace `http://xnatworks.io/dxreport`, root `dxreport:SessionReport`) is what the FHIR
  report poller (`xnat_fhir_report_plugin`) writes for reports a vendor's FHIR server sends:
  HOPPR's on the RSNA demo. A card's report in the same datatype shows on the session page next
  to a vendor's and is compared in the same columns. A new `analysis:` field would have needed
  a schema release and a viewer of its own.
- **The mapping is the poller's, ported, not reinvented.** `parse` follows
  `DiagnosticReportParser`, and `build_values`, `record_label` and `report_kind` follow
  `DxReportRecordMapper` (label `<session>_RPT_<tail(source)>_<id>`, cap 64; `AI_DRAFT` when a
  Device authored it and no human performed it). A report reads the same whichever route filed it.
- **Rejected: an ingest endpoint in the FHIR plugin, which the card would POST to.** The plugin
  is a poller and has no inbound endpoint. Adding one would put a second write path into the
  plugin and make every card that writes a report depend on a plugin that only some sites have.
  proc-wrapup already holds the XNAT session, knows the card and the run status, and publishes
  create-only records. The same holds for the model registration in 0.7.1.
- **Rejected: the card's own image writing the record.** Cards do not hold XNAT credentials and
  do not talk to XNAT. The tool writes files and the wrapups publish them. A card that wants a
  report record only has to write FHIR.

## The contract

- The card's results block names the file: `"diagnosticReport": "<path in the tool's output>"`.
  The workshop turns this into `XNW_DIAGNOSTIC_REPORT` on the command, and the JSON contract key
  is `diagnostic_report`. `declared_path` refuses an absolute path or one containing `..`.
- The file must be a FHIR R5 `DiagnosticReport` with an `id`. **The id is the card's
  responsibility and should be deterministic** (NV-Reason-CT hashes the model revision, the input
  digest, the region and the prompts). The label is built from it, and a label that already exists
  is taken as "this report is already filed" (probe 200 → `{"exists": true}`), which is what makes
  a rerun idempotent. A random id would file a fresh record on every rerun.
- The model is a contained `Device` **that the findings name as their `device`**, with no performer
  for it. FHIR R5 allows only people and organisations as a report's `performer` (Codex P2,
  container-workshop PR #65), and HOPPR's report has this shape. A Device a performer entry
  references is tolerated as the author too, and such a performer is not a person even with a
  display. The record's `device_name`/`device_version` come from that Device. A contained Device
  that nothing points at (a scanner, say) does not make a human's draft an `AI_DRAFT` (Codex P1,
  PR #23). This is stricter than the poller as ported, which takes the first contained Device. A
  vendor report filed by the poller and the same document filed here can differ in `report_kind`
  only when the report carries a Device that neither a finding nor a performer names. The limit:
  a finding whose `device` is the scanner (FHIR allows that for a measurement) reads as the
  model's.

## What the record holds, and where each value comes from

| Field | From |
|---|---|
| project, `imageSession_ID`, label prefix | the archived session (`GET /data/experiments/<id>`), never the document |
| `study_instance_uid`, `modality` | the session's `UID` and `modality`. The document's own `ImagingStudy` UID is the fallback only |
| `pseudonym` | the subject's label (`GET /data/subjects/<id>`), as the poller records it |
| `source_system` | `urn:xnatworks:card:<card id>` (the card id, `nv-reason-ct`, is in the label) |
| `source_id`, `source_version` | the report's `id`, `meta.versionId` (default `1`) |
| `report_kind` | `AI_DRAFT` / `FINAL_SIGNED` / `HUMAN_DRAFT` (the poller's rule, with the Device exception above) |
| `conclusion`, `narrative` | `conclusion`. The first base64 `presentedForm`, else the untagged `text.div` |
| `findings` | the contained `Observation`s `result` references; every contained one only when the report has no `result` at all (a `result` naming external observations, by reference or by `identifier` alone, means the contained ones only support: Codex P2, PR #23): code, display, valueString or first note, valueQuantity, a component named confidence or instance number, bodySite |
| `pseudonymization` | a sentence saying the card wrote the report inside XNAT from the archived session |
| `raw_json` | the document, when it is under 200,000 characters |
| `REPORT` resource | the document, whole, as `diagnosticreport-<id>-v<version>.json` |

## Traps

- **A value outside the XSD's enumerations fails the whole create**, not just that field.
  Status and category are clamped (`unknown` and `OTH`), and `report_kind` and `source_format`
  are always ours. Text over a cap (4,000 per finding text and value, 8,000 conclusion, 100,000
  narrative) is cut with a warning, and `raw_json` over 200,000 is left off, because the
  `REPORT` file keeps the full text.
- **Every plain `xs:string` element is a `varchar(255)` column** in XNAT, `xs:float` is single
  precision and `xs:int` is 32-bit, and one value outside its column fails the whole create. Codes,
  displays, units, body sites, the performer, the device, the version and the id are cut to 255
  characters. A number that is NaN, infinite or beyond single precision, and an instance number
  beyond 32 bits, are left off, each with a warning. The existence probe compares the id as the
  record holds it, so a cut id still finds its own record.
- **The `REPORT` file name is a name, not a path.** The id and `meta.versionId` are both
  sanitised, so a versionId of `1/../x` cannot put a separator into it.
- **Session labels get the same treatment as ids**: a label XNAT can carry as it is (letters,
  digits, `_`, `-`) is used as it is, and any other gets a hash beside its rewritten form, so
  `SUB.01` and `SUB_01` stay apart (Codex P2, PR #23).
- **`xs:dateTime` will not take a FHIR date alone** (`2026-10-05`). `issued` and `effective`
  are left off unless they carry a time.
- **The label keeps its report identity under the 64-character cap.** The poller cuts the whole
  label from the right, so with a 60-character session label every report on the session became
  `<label>_RPT` and the existence probe took each new report for one already filed (Codex P1,
  PR #23). Here the `_RPT_<source>_<id>` end is kept whole, and the session label is shortened,
  with a 6-character hash of the whole of it so two long labels sharing a beginning stay apart. An
  id too long for the end is replaced by a 16-character hash. An id the label cannot spell as it is
  (anything but letters and digits: `a-b` and `a.b` would both be `a_b`) gets an 8-character hash
  of the real id beside its readable form. Card ids, which are hex, give the poller's label
  character for character.
- **A label is a lossy spelling of the identity, so the existence probe reads the record it
  finds.** A 200 is "already filed" only when that record's `source_id` and `source_system` are
  this report's. Any other record there is a collision, reported as an `error`, never a report
  silently dropped (Codex P1, PR #23).
- **A performer signs whether or not it is named.** FHIR leaves `Reference.display` optional, so a
  final report whose performer is `{"reference": "Practitioner/123"}` is `FINAL_SIGNED`, and the
  record's `performer` is the best name there is: the display, else a contained Practitioner's
  HumanName (text, else given and family), Organization's or CareTeam's name, or a
  PractitionerRole's practitioner display, else the reference itself, else the identifier's value.
  A Device performer, contained or by URL or by `type`, and a `#` reference to nothing contained
  name nobody (Codex P2, PR #23). The poller as ported reads the display only, so the same signed
  report without a display reads as a draft there.
- **A Device the findings or a performer name is the author whether or not it is named.** FHIR
  leaves `Device.name` optional, so the AI test is "a contained Device is pointed at", not "the
  record has a device name" (Codex P2, PR #23).
- **The card is in the label as it is**, not as the poller's first 12 alphanumerics: `a-b` and `ab`
  were one label, and the second card's report a collision it could never file (Codex P2, PR #23).
  A card id over 16 characters, or one the label cannot spell, is shortened with a hash of the
  source. So the label reads `HB0004_1_RPT_nv-reason-ct_<id>`, where the poller would write
  `nvreasonct`.
- **A create race is not retried under a random label.** Two wrapups filing the same report at once
  both see the label free. The second create gets a 409, and the generic publisher would retry
  under a random suffix and file the report twice. A race the publisher's own probe sees ("label
  ... already exists") is the same case. `publish_record(retry_on_conflict=False)` hands
  the 409 back, and the record under the label is read as on a probe: the same report is
  `exists`, another is a collision (Codex P2, PR #23). A race on the same session is narrower.
  There XNAT answers the second PUT as an update of the record the first just made, which the
  create-only publisher cannot rule out.
- **JSON numbers can be too large for a float** (thousands of digits). Such a value is left off the
  finding with a warning instead of raising `OverflowError` (Codex P1, PR #23).
- **A character XML 1.0 forbids** (`json.loads` accepts `"\u0001"`) is written as its escape
  (`\x01`), with a warning, by the same `_xml_text` the analysis records use. Otherwise one such
  character makes the whole create document malformed (Codex P2, PR #23). This is done before
  the caps, so a cap counts what is written.
- **A malformed nested value** (`presentedForm: ["oops"]`, a string where a CodeableConcept goes)
  is an `error` outcome, not an exception. So are a document nested past Python's recursion limit
  (`RecursionError` from `json.loads`) and a truncated XNAT answer (`http.client.IncompleteRead`,
  which is not an `OSError`). It is caught after the run record exists, so it must not
  stop `wrapup.json` or the pointer reduction (Codex P1, PR #23).
- **Labels are unique per project, not per session.** The session label in the prefix keeps
  them apart. A 409 from a race takes the usual one retry with a random suffix, and `_relabel`
  now recognises the `dxreport:` root, so the document's `label` attribute matches the URL it
  is PUT to.
- **The run record comes first and is never rolled back** for the report's sake. A report that
  fails to file is an outcome under `wrapup.json` `diagnostic_report` (`skipped`, `exists` or
  `error`). The copy of `wrapup.json` already uploaded to the run record predates the report,
  so the local manifest and the pointer carry the outcome. The same holds for `trained_model`.

## Limits

- Session scope only. A subject- or dataset-scoped report has no `dxreport` datatype to go in.
- One report per run. A tool that writes several needs a list in the contract, which does not exist yet.
- No signing, no status workflow, no FHIR write-back. Review of an AI draft happens elsewhere
  (the poller's `review_state` fields are left empty).
- Needs `xnat-dxreport-schema-plugin` on the site. Without it the create fails and the outcome
  says so, while the run record is unaffected.

## Tests

`tests/test_dxreport.py` covers parsing (Device versus human performer, findings, narrative,
the simulated tag, refusals), values (clamping, caps, archive facts over document claims, dates),
the XML (XSD order, escaping, a 409 relabel) and publishing with XNAT faked (filed, exists,
scope, failed run, missing file, bad document, failed create). `tests/test_proc.py` runs the
whole wrapup end to end through the fake XNAT: declared, not declared, failed run.
