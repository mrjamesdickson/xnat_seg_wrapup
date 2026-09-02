# Requirements — xnat_seg_wrapup

The shared post-processing step for every wrapped segmentation model in the XNAT
model catalog (see the epic in `xnatworks/business/ai-foundation-models/EPIC-MODEL-CATALOG.md`).
Runs as a Container Service wrapup command; upstream model images stay untouched.

| # | Requirement | Status | Notes |
|---|---|---|---|
| R1 | Run as a `docker-wrapup` command: no mounts/inputs/outputs declared, reads the parent's output at `/input`, writes the upload set to `/output` | **Done** | Live on demo02 as command 674, 2026-09-02. No `ENTRYPOINT` in the image: CS runs the command-line without overriding it |
| R2 | Per-structure volumes (mL from the image's own spacing) as `volumes.json` and `volumes.csv` | **Done** | 117-structure TotalSegmentator output measured on the CPTAC scan |
| R3 | Self-contained HTML volumetrics report, light and dark, no external assets, research-only footer | **Done** | Rendered in a browser: pending (never visually checked; markup and tests only) |
| R4 | Viewer label files: ITK-SNAP `labels.txt` and 3D Slicer `labels.ctbl`, stable golden-angle colours mirrored as chips in the report | **Done** | |
| R5 | Label names from the model's own table: `labels.json`, nnU-Net `dataset.json` (v1/v2), MONAI `metadata.json`, ITK-SNAP, CSV; auto-discovered under `/input` | **Done** | TotalSegmentator card writes `labels.json` from its `class_map` inside the model container |
| R6 | Merge one-file-per-structure binary masks into one label map (TotalSegmentator default layout), reproducible label assignment, overlaps logged | **Done** | Auto-detected; `--merge no` to disable |
| R7 | DICOM SEG from the mask and the source series when the parent copied it to `/output/.source_dicom`; masks on a different grid refused, never resampled | **Done** | BINARY type; a 117-segment × 203-frame SEG is ~116 MB. LABELMAP encoding would be far smaller: see R9 |
| R8 | Failure policy: no mask → fail; report/label/SEG failure → logged, masks still delivered | **Done** | Tested |
| R9 | LABELMAP segmentation type (DICOM 2024) to shrink multi-structure SEGs; keep BINARY where the viewer cannot read LABELMAP | Not started | OHIF support to be verified first |
| R10 | Register the SEG as an ROI collection so OHIF lists it (`/xapi/roi/...`) | Not started | Needs project/session, which a wrapup does not receive |
| R11 | RTStruct output option | Not started | |
