# Prompt history

## 2026-09-02 — xnat_seg_wrapup

- "create the wrapper container git repo and we can proceed" (after planning the
  model-catalog epic from the Nalvera screenshot; James: "most of these can be
  deployed xnat containers... wrap them in command json", "we can just add them to
  the container marketplace"). Created this repo: the shared wrapup image that gives
  every wrapped model the same report, label files, and DICOM SEG.

## 2026-09-08 — xnat_seg_wrapup

- Implement plan decision D20 in the publisher: "raw/sub-H025.html should be in with
  everything else" → "logs good, report standard did it run report good, provenance good,
  DERIVED should contain the entire output of the container as we expect" → "why are we
  copying files outside derived" → "we have a predefined dataset that's created by the
  scientists. Don't fuck it up." Result: 0.6.0, `feature/roles-as-views`: DERIVED is the
  tool's whole tree at the resource root, four fixed resources, METRICS a view (role → DERIVED
  paths in wrapup.json/results_json), record-fetch resolves views, pointer-only drops the
  empty files it skipped. Design: `docs/ROLES-AS-VIEWS.md`.
