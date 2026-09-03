# Requirements — xnat_seg_wrapup

The shared post-processing step for every wrapped segmentation model in the XNAT
model catalog (see the epic in `xnatworks/business/ai-foundation-models/EPIC-MODEL-CATALOG.md`).
Runs as a Container Service wrapup command; upstream model images stay untouched.

| # | Requirement | Status | Notes |
|---|---|---|---|
| R1 | Run as a `docker-wrapup` command: no mounts/inputs/outputs declared, reads the parent's output at `/input`, writes the upload set to `/output` | **Done** | Live on demo02 as command 674, 2026-09-02. No `ENTRYPOINT` in the image: CS runs the command-line without overriding it |
| R2 | Per-structure volumes (mL from the image's own spacing) as `volumes.json` and `volumes.csv` | **Done** | 117-structure TotalSegmentator output measured on the CPTAC scan |
| R3 | Self-contained HTML volumetrics report, light and dark, no external assets, research-only footer | **Done** | Rendered in headless Chromium (light and dark) 2026-09-02 from the live TotalSegmentator output: 78 rows, chips, bars, footer all correct |
| R4 | Viewer label files: ITK-SNAP `labels.txt` and 3D Slicer `labels.ctbl`, stable golden-angle colours mirrored as chips in the report | **Done** | |
| R5 | Label names from the model's own table: `labels.json`, nnU-Net `dataset.json` (v1/v2), MONAI `metadata.json`, ITK-SNAP, CSV; auto-discovered under `/input` | **Done** | TotalSegmentator card writes `labels.json` from its `class_map` inside the model container |
| R6 | Merge one-file-per-structure binary masks into one label map (TotalSegmentator default layout), reproducible label assignment, overlaps logged | **Done** | Auto-detected; `--merge no` to disable |
| R7 | DICOM SEG from the mask and the source series when the parent copied it to `/output/.source_dicom`; masks on a different grid refused, never resampled | **Done** | BINARY type; a 117-segment × 203-frame SEG is ~116 MB. LABELMAP encoding would be far smaller: see R9 |
| R13 | DICOM SEG on de-identified series: Type 2 patient/study attributes that an anonymiser stripped (TCIA drops `PatientBirthDate` etc.) are added empty before highdicom copies them, instead of failing the SEG | **Done** (0.2.1) | Found on the first MuscleMap run (STS_PETMR); two tests in `tests/test_dicomseg.py` |
| R14 | One colour per structure everywhere: DICOM SEG segments carry `RecommendedDisplayCIELabValue`; a BIDS `.tsv` lookup beside each map for the XNAT workbench; an 8-bit renumbered companion (+ `.tsv`) when values exceed 255 | **Done** (0.2.2) | Six tests across `test_labels.py`, `test_volumes.py`, `test_cli.py`, `test_dicomseg.py` |
| R8 | Failure policy: no mask → fail; report/label/SEG failure → logged, masks still delivered | **Done** | Tested |
| R9 | LABELMAP segmentation type (DICOM 2024) to shrink multi-structure SEGs; keep BINARY where the viewer cannot read LABELMAP | Not started | OHIF support to be verified first |
| R10 | Register the SEG as an ROI collection so OHIF lists it (`/xapi/roi/...`) | **Done** | Context arrives as environment from the parent's derived inputs (`SEG_PROJECT`, `SEG_SESSION_ID`, `SEG_SCAN_ID`) plus CS's `XNAT_*`; failure logged, not fatal |
| R15 | Drop `segmentation.seg.dcm` from the delivered resource once the ROI collection has been registered with the same bytes; keep it whenever no collection was created (registration skipped, no XNAT context, or failed), and `--keep-seg-file` to override | **Done** (0.2.4) | The collection is the SEG's home — OHIF's segmentation badge is driven by the collection, not by the file on the scan. Keeping both duplicated ~20 MB a run (measured on a 512x512x50 MR station). Four tests in `tests/test_register.py`, including the three keep-it paths |
| R11 | RTStruct output option | Not started | |
| R12 | Merge one-multilabel-map-per-model outputs (MOOSE) with label offsets; per-mask sidecar label files; MOOSE `organ_indices.json` format | **Done** (0.2.0) | Eight tests in `tests/test_multilabel_merge.py` |
