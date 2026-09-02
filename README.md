# xnat_seg_wrapup

Shared post-processing for XNAT segmentation containers, packaged as a
[Container Service](https://wiki.xnat.org/container-service/) **wrapup command**.
Any model container that writes a NIfTI mask gets, without changing its image:

| File | Contents |
|---|---|
| `segmentation.nii.gz` (or the model's own files) | The mask, merged to one label map when the model wrote one file per structure |
| `report.html` | Self-contained volumetrics report: total volume, structure count, per-structure table with proportional bars; light and dark |
| `volumes.json` / `volumes.csv` | Machine-readable volumes, voxel size, matrix, voxel counts |
| `labels.txt` / `labels.ctbl` | ITK-SNAP label file and 3D Slicer colour table, same indices and colours as the report chips |
| `segmentation.seg.dcm` | DICOM SEG built from the mask and the source series, when the source DICOM is available (see below) |
| `wrapup.json` | What the wrapup did: inputs, label source, merge, SEG segment mapping |

This is the one piece of new code behind the model catalog epic: upstream images
(TotalSegmentator, MOOSE, MuscleMap, MONAI bundles) are wrapped by a `command.json`
and this wrapup, so every card produces the same resource layout.

## How a wrapup command works

Verified against the Container Service source (`CommandResolutionServiceImpl`,
`Command.validateDockerSetupOrWrapupCommand`):

- A wrapup command has `"type": "docker-wrapup"` and **must declare no mounts,
  inputs, or outputs**; validation rejects any.
- The service mounts the parent command's output mount at **`/input`** and a fresh
  build directory at **`/output`**. Only `/output` is uploaded to XNAT.
- The service injects `XNAT_HOST`, `XNAT_USER`, `XNAT_PASS` (alias token),
  `XNAT_WORKFLOW_ID`, `XNAT_EVENT_ID`, as for any container.
- The wrapup is found **by image name** (`dockerService.getCommandByImage`), so the
  image must already be registered as a command. Pulling the image registers it from
  the `org.nrg.commands` label in the Dockerfile; or `POST /xapi/commands` with
  `commands/seg-wrapup.json`.
- The wrapup never sees the parent's *input mounts* (e.g. the source DICOM); if it
  needs those files the parent command-line copies them into its own output mount.
  It **does** receive the parent's resolved `environment-variables` (CS copies them
  onto the wrapup container) and its `command-line` is resolved against the parent's
  replacement keys. A parent that declares `project-id`/`session-id`/`scan-id`
  derived inputs can therefore hand the launch context to the wrapup as
  `SEG_PROJECT=#PROJECT_ID#`, `SEG_SESSION_ID=#SESSION_ID#`, `SEG_SCAN_ID=#SCAN_ID#`.
- A parent output handler opts in with `"via-wrapup-command": "xnatworks/seg-wrapup:0.1.0"`.
- CS runs the wrapup's `command-line` **without overriding the image entrypoint**.
  This image therefore has no `ENTRYPOINT`, only `CMD ["seg-wrapup"]`; with an
  entrypoint the container ran `seg-wrapup seg-wrapup` and exited 2 on the first
  live run.

### The DICOM SEG needs the source series

The wrapup only sees the parent's output. To get a DICOM SEG, the parent command
copies its source DICOM into `/output/.source_dicom` (any image with `sh` can do
`... && cp -r /input /output/.source_dicom`). The wrapup consumes that directory
and does **not** forward it to `/output`, so nothing is re-uploaded. Without it the
run still succeeds and simply has no SEG.

The mask is aligned to the series grid by axis permutation and flip only, which is
what a dcm2niix-derived NIfTI needs. A mask on a different grid is refused with a
clear error rather than resampled; a silently resampled overlay is worse than none.
Sparse label values are renumbered to consecutive DICOM segment numbers; the mapping
is in `wrapup.json`.

### The SEG is registered with OHIF when the context is present

When the environment carries `SEG_PROJECT` and `SEG_SESSION_ID` (from the parent's
derived inputs, see above) plus the `XNAT_HOST`/`XNAT_USER`/`XNAT_PASS` that CS
injects, the wrapup `PUT`s the SEG to
`/xapi/roi/projects/{project}/sessions/{session}/collections/{label}?type=SEG&overwrite=true`,
which is what the OHIF viewer plugin lists as an ROI collection. The label is
`<model>_scan<id>_<UTC stamp>` unless `--roi-label` / `SEG_ROI_LABEL` is set. A
registration failure is logged and recorded in `wrapup.json`; the SEG file still
ships in the resource. `--no-register` turns it off.

## Failure policy

A missing or unreadable mask fails the run: there is nothing to upload. A report,
label-file, or SEG failure is logged with context and the masks still ship, because a
segmentation without a report is useful and a failed workflow with a good
segmentation stranded in a build directory is not.

## Usage

```
seg-wrapup [--input /input] [--output /output] [--model NAME] [--model-version V]
           [--labels FILE] [--source-dicom DIR] [--merge auto|yes|no] [--no-dicom-seg]
           [--session LABEL] [--scan ID] [--no-register] [--roi-label LABEL]
```

Every flag has an environment variable (`SEG_INPUT`, `SEG_OUTPUT`, `SEG_MODEL_NAME`,
`SEG_MODEL_VERSION`, `SEG_LABELS`, `SEG_SOURCE_DICOM`, `SEG_MERGE`,
`SEG_SESSION_LABEL`, `SEG_SCAN_ID`), so a parent command can set them without a
custom command line.

**Label tables** are read from, in order: `--labels`, then the first of
`labels.json`, `dataset.json` (nnU-Net v1 or v2), `metadata.json` (MONAI bundle
`channel_def`), `labels.txt` (ITK-SNAP), `labels.csv` found under `/input`.
Ship the model's label table next to its masks and the report names every structure.

**Merging**: when several masks are present and each holds only `{0, 1}` (the
TotalSegmentator one-file-per-structure layout), they are merged into
`segmentation.nii.gz`, labels taken from the declared table where a filename
matches, otherwise assigned sequentially in sorted-filename order. Later files win
overlaps, and overlaps are logged.

## Example parent command

`commands/examples/totalsegmentator-with-wrapup.json` is a parent command on the
upstream `wasserth/totalsegmentator:2.18.0` image using this wrapup, run live on
demo02 (command 675, wrapper 821) on 2026-09-02. Two things it encodes that cost
a launch each to learn:

- `override-entrypoint: true` makes the Container Service run the command line as
  `/bin/sh -c "<command-line>"`, so `&&` chains work and you must **not** wrap the
  line in your own `sh -c`.
- Upstream images that are not built on a PyTorch/NVIDIA base do not set
  `NVIDIA_VISIBLE_DEVICES`, and without it the nvidia runtime exposes no GPU: the
  first run reported "No GPU detected. Running on CPU." Set
  `NVIDIA_VISIBLE_DEVICES=all` and `NVIDIA_DRIVER_CAPABILITIES=compute,utility` in
  the command's `environment-variables`.

The label table is produced inside the model container from TotalSegmentator's own
`class_map`, so the report names all 117 structures without a copy of the map in
this repo.

## Development

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -e ".[test]"
.venv/bin/python -m pytest
docker build -t xnatworks/seg-wrapup:0.1.0 .
docker run --rm -v /path/to/model-output:/input:ro -v /tmp/out:/output xnatworks/seg-wrapup:0.1.0
```

Tests cover label-file parsing for each format, volume arithmetic, merging, the
DICOM SEG round trip through highdicom (including a flipped-and-permuted mask), the
CLI's failure policy, and that the Dockerfile label matches `commands/seg-wrapup.json`.

## Not yet

- RTStruct output.
- Resampling masks on a different grid (deliberately refused).

## Licensing

Apache 2.0. This image contains no model weights. Research and decision support
only; not a medical device.
