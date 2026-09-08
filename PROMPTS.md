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

## 2026-09-08 — xnat_seg_wrapup (0.6.1)

- Fix a fidelity gap in 0.6.0 against D20 ("DERIVED is the scientists' derivatives dataset
  byte-for-byte, path-for-path"): `_tree()`/`_is_hidden()` and `copy_raw_output` skipped every
  dot-prefixed entry, so a dataset's `.bidsignore` (the reference QSIRECON on demo02
  XNAT_E09349 has one) and `.heudiconv/` never reached DERIVED. Keep the `.source_dicom`
  exclusion by name, enumerate the wrapup's own names, no dot-prefix rule inside the tool tree.
  TDD, bump to 0.6.1, one PR, Codex loop. Result: `fix/derived-keeps-dotfiles`,
  `RESERVED_ROOT_NAMES` in `segwrapup/execution.py`.
