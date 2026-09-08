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
| `<mask>.tsv` | BIDS `_dseg.tsv` lookup (index, name, #colour) beside each label map; what the XNAT workbench reads for a mask |
| `<mask>_uint8.nii.gz` / `<mask>_uint8.tsv` | Only when the map holds values above 255 (MuscleMap's 1101…8162): the same map renumbered 1..N as a byte, with its lookup carrying the original colours, for viewers whose drawing layer is 8-bit. The `new → original` mapping is in `wrapup.json` |
| `segmentation.seg.dcm` | DICOM SEG built from the mask and the source series, when the source DICOM is available (see below). **Removed from the resource once it has been registered as an ROI collection**, which holds the same bytes; it stays when no collection was created, or with `--keep-seg-file` |
| `wrapup.json` | What the wrapup did: inputs, label source, merge, SEG segment mapping |

This is the one piece of new code behind the model catalog epic: upstream images
(TotalSegmentator, MOOSE, MuscleMap, MONAI bundles) are wrapped by a `command.json`
and this wrapup, so every card produces the same resource layout.

## Analysis record (since 0.3.0)

When the card opts in, the wrapup publishes an `analysis:sessionAnalysisData` record on the
session after the ROI collection. The record's **fields** are type, status, QC and provenance
only; no measurement is ever a field. The record's **resources carry the entire output of the
run**, and since 0.6.0 there are exactly four of them (`docs/ROLES-AS-VIEWS.md`):

| Resource | Holds | seg-wrapup | proc-wrapup |
|---|---|---|---|
| `DERIVED` | the tool's output, complete, unchanged, at the resource root; never a wrapup file | the delivered mask(s), their `.tsv` sidecars and 8-bit companions, `volumes.json`/`volumes.csv`, the DICOM SEG when kept, and the tool's other output under `raw/` | the tool's whole `/output` tree as the tool laid it out (`dataset_description.json`, `sub-<label>/...`) |
| `REPORT` | the wrapup's `report.html` and nothing else | volumetrics report | did-it-run report, linking the tool's own HTML reports inside `DERIVED` |
| `PROVENANCE` | what the wrapup knows about the run | `wrapup.json`, `labels.txt`, `labels.ctbl` | `wrapup.json`, `status.json`, `prereq.json` |
| `LOGS` | the parent container's captured stdout/stderr | – | `logs/stdout.log`, `logs/stderr.log` |

`METRICS` is **not a resource**: it is a *view*, a list of paths inside `DERIVED` (seg-wrapup:
`volumes.json`, `volumes.csv`, `segmentation.tsv`; a proc-wrapup card names its own globs). Views
are written to `wrapup.json` (`"views": {"METRICS": [...]}`) and to the record's `results_json`,
so the record page, record-fetch and any script resolve a role to files without a second copy
of anything. The same files are also on the parent's resource (`output_resource_label`) for
OHIF and the viewer sidecars unless the card runs proc-wrapup with `--pointer-only`; the DICOM
SEG registered as a ROI collection is not copied again.

The card opts in through environment variables on its command, which the registry installer
writes from the card's `results` block. The Container Service stores each value in a 255-char
column, so the contract is **discrete variables**: `XNW_CARD_ID`, `XNW_CARD_REVISION`,
`XNW_CONTRACT_VERSION`, `XNW_ANALYSIS_TYPE`, `XNW_CONTAINER_IMAGE`, `XNW_CONTAINER_DIGEST`,
`XNW_OUTPUT_RESOURCE_LABEL`, `XNW_SUPERSEDES_ID`, and optional `XNW_RESOURCE_<ROLE>=a,b`
view globs, relative to the `DERIVED` root (`XNW_RESOURCE_METRICS=sub-*/**/*.json`). A glob for
one of the four fixed resources is ignored with a warning and listed under `ignored_overrides`
in `wrapup.json`: the wrapup decides what they hold, and nothing the tool wrote is ever copied
out of `DERIVED` (a tool's HTML report copied on its own loses the figures it links by relative
path). A single `XNW_CONTRACT` JSON value is still honoured where it fits.

Worked example, an mriqc card with `XNW_RESOURCE_METRICS=sub-*/**/*.json`: the record's
`DERIVED` holds `dataset_description.json`, `sub-H025.html`, `sub-H025_ses-H025_T1w.html`,
`sub-H025/ses-H025/anat/sub-H025_ses-H025_T1w.json`, the `figures/` and everything else mriqc
wrote, exactly as it wrote it; `REPORT` holds proc-wrapup's `report.html`, which links the
mriqc reports; `PROVENANCE/wrapup.json` says `"views": {"METRICS": ["sub-H025/ses-H025/anat/sub-H025_ses-H025_T1w.json", ...]}`.
A downstream card with `XNW_PREREQ_IQM=pipeline=mriqc;role=METRICS` gets exactly those files at
`prereq/iqm/sub-H025/ses-H025/anat/...`. What this will **not** do: rename, move, filter or
reformat a tool file; put a wrapup file in `DERIVED`; upload a zero-byte file anywhere (XNAT
refuses it; it is listed as `skipped_empty`); or honour a card's globs for the four fixed resources.

No `XNW_*` variables, no record; nothing else changes. Publishing is create-only and
additive: a label that already exists on the session is refused before any write, a file
upload that fails after the create deletes the new record again, and every failure is logged
and written to `wrapup.json` under `analysis_record` while the masks, report and ROI
collection still ship. `auto_qc_status` is `WARN` when any delivered mask could not be
measured. The record label is the ROI collection's label plus `_record`
(`<model>_scan<id>_<UTC stamp>_record`), overridable with `--record-label` / `SEG_RECORD_LABEL`;
`--no-publish` / `SEG_NO_PUBLISH` switches it off. The datatype is provided by
`xnat-analysis-schema-plugin`; design in `development/xnat_genericProcessing_plugin/docs/`.

Contract keys: `card_id`, `card_revision`, `contract_version`, `analysis_type`,
`container_image`, `container_digest`, `output_resource_label`, `supersedes_id`, and
`resources` (view role -> list of file globs relative to the `DERIVED` root, merged over the
wrapup's defaults; entries for `DERIVED`, `REPORT`, `PROVENANCE`, `LOGS` are ignored).

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
- A parent output handler opts in with `"via-wrapup-command": "xnatworks/seg-wrapup:0.6.1"`.
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
is in `wrapup.json`. Every segment carries `RecommendedDisplayCIELabValue`, the same
colour as the report chips and the label files, so OHIF shows structures in the
colours the rest of the resource uses.

### One-segment SEGs need the segment identification per frame

`SegmentIdentificationSequence` is moved out of `SharedFunctionalGroupsSequence` and copied
into every per-frame group before the SEG is written.

highdicom hoists a functional group macro into the shared groups whenever its value is the
same for every frame, which for a **one-label mask is always true**. The file is valid
DICOM either way, but XNAT's ROI plugin reads that macro from the per-frame groups only and
rejects the collection with `HTTP 500 SegmentIdentification missing`. The effect was
invisible until the first lesion model: a 95-segment FastSurfer SEG registered fine, because
its referenced segment number varies per frame and so stayed where XNAT looks.

The macro is moved, not copied — DICOM forbids the same functional group macro appearing in
both the shared and the per-frame groups. On the multi-segment path there is nothing in the
shared groups to move and the step is a no-op.

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

**Merging** (two cases, both automatic unless `--merge no`):

- *One file per structure*, each holding only `{0, 1}` (TotalSegmentator's default
  layout): merged into `segmentation.nii.gz`, labels from the declared table where a
  filename matches, otherwise assigned sequentially in sorted-filename order.
- *One multilabel map per model*, each with its own label file beside it sharing the
  filename prefix (MOOSE: `clin_CT_organs_segmentation_X.nii.gz` next to
  `clin_CT_organs_organ_indices.json`): merged with label offsets so every structure
  keeps a unique index and its name, and one SEG covers the whole launch.

Later files win overlaps in both cases, and overlaps are logged. A single map with a
sidecar label file uses that sidecar automatically. MOOSE's `organ_indices.json`
format is read natively.

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
docker build -t xnatworks/seg-wrapup:0.6.1 .
docker run --rm -v /path/to/model-output:/input:ro -v /tmp/out:/output xnatworks/seg-wrapup:0.6.1
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

## Labels

ROI collections and records are XNAT experiments, whose labels are unique per project. The
default label is `<pipeline>_<session label>_scan<id>_<UTC stamp>` (since 0.4.1; the session
id when its label cannot be read), so two sessions' runs of one pipeline finishing in the same
second cannot collide; a 409 on create is retried once with a random suffix.

## record-fetch: prerequisites resolved at launch (since 0.5.0)

A setup command (`xnatworks/record-fetch:<version>`, attached to a card's root resource input
with `via-setup-command`). It reads one `XNW_PREREQ_<NAME>` variable per prerequisite
(`key=value;…`): `type=`/`pipeline=` pick a generic record on the session (newest SUCCEEDED;
`accepted=true` requires review; `min=` a version floor; `id=` names one record), `role=` the
record resource to copy (default `DERIVED`); `resource=LABEL` copies a session resource
instead. Files land under `prereq/<name>/` beside the passed-through input, `prereq.json`
says what was chosen, and an unmet prerequisite exits 3 so the Container Service reports
`Failed (Setup)` and never starts the tool. The same card therefore runs by hand, in an
orchestration or from an event rule and always finds its own inputs.

Clause reference (`XNW_PREREQ_<NAME>` = `key=value;…`; unknown keys and malformed clauses are
refused at parse time so a misspelling can never widen the match). `<NAME>` is a letter followed
by letters, digits or underscores (at most 64 characters), lowercased into `prereq/<name>/`; two
names that differ only in case are refused. A prerequisite is either a resource (`resource=`, with
`scope=`/`scan_type=`) or a record (`type=`, `pipeline=`, `min=`, `role=`, `accepted=`, `id=`);
mixing the two is refused rather than silently taking the resource and dropping the record rules.
A key given twice is refused (the later value used to win), and `scan_type=` requires `scope=scan`:

| Key | Meaning | Default |
|---|---|---|
| `type=` | record `analysis_type` to match | any |
| `pipeline=` | record `pipeline_name` to match | any |
| `min=` | minimum `pipeline_version` (numeric-aware compare) | none |
| `accepted=` | `true`: only records with `review_state ACCEPTED`; `false`: newest SUCCEEDED | `false` |
| `id=` | one explicit record id; wins over the rules above | none |
| `role=` | what to copy from the record: a resource (`DERIVED`, `REPORT`, `LOGS`, `PROVENANCE`) or a view (`METRICS`, any role the producing card named): the `DERIVED` files its `wrapup.json` view lists, at their `DERIVED` paths. A record published before 0.6.0 has no views and its resource of that name is used instead | `DERIVED` |
| `resource=` | a session (or scan) resource label instead of a record | none |
| `scope=` | `session` or `scan` (with `resource=`) | `session` |
| `scan_type=` | with `scope=scan` on a session-level run: glob over scan `type` (`T1*`) | the run's scan |

A prerequisite needs at least one of `resource=`, `id=`, `type=`, `pipeline=`. Only SUCCEEDED
records are ever chosen; FAILED records (including the failure records record-fetch itself
writes) never satisfy a prerequisite. The card's own `metadata.json` uses the same fields in
camelCase (`analysisType`, `minVersion`, `scanType`); the installer writes the variables.

`resource=LABEL;scope=scan` takes the resource from a **scan** instead (dcm2niix needs the
scan's `DICOM`): on a scan-level run the run's scan (`PROC_SCAN_ID`); on a session-level run
`scan_type=<glob>` selects the scans (`T1*`, `BOLD`), each landing under `prereq/<name>/<scan>/`.
A scan with an empty resource, or no scan of the type, is an unmet prerequisite with that reason.

An unmet prerequisite is also **recorded**: when the card's `XNW_*` contract is in the
environment (it is, the setup command inherits the card's variables), record-fetch publishes a
`analysis:sessionAnalysisData` record with `run_status FAILED`, auto QC `FAIL`, the reason in
`notes` (`Not run: prerequisite(s) unmet at setup: preproc: no fake-preprocessing/fake-preproc
record on this session`), the full resolution in `inputs_json` and `prereq.json` under
`PROVENANCE`. The Container Service alone only says `Failed (Setup)`; the record says why, on the
session, where the reviewer looks. A FAILED record never satisfies a prerequisite.

## proc-wrapup: the generic wrapup (since 0.4.0)

For cards that are not segmentations (QC pipelines, diffusion, radiomics, anything). Same
image lineage, entrypoint `proc-wrapup`, image `xnatworks/proc-wrapup:<version>`
(`Dockerfile.proc`). It interprets nothing:

- copies everything the tool wrote to `/output/raw/`, dotfiles included (`.bidsignore`,
  `.heudiconv/` are part of a dataset; only the DICOM copy at `.source_dicom`, which XNAT
  already holds, is left out) and publishes that tree as the record's `DERIVED` **at the resource root**, no
  `raw/` segment: the scientists' derivatives dataset, byte-for-byte and path-for-path. The
  local `raw/` only keeps the tool's files apart from the wrapup's own in `/output` (a tool that
  writes its own `report.html` or `logs/` must not collide with the wrapup's);
- reads `status.json` if the card's command line trapped a failure (`…; rc=$?; echo
  "{\"exit_code\": $rc, \"workflow_id\": \"$XNAT_WORKFLOW_ID\"}" > /output/status.json; exit 0`,
  because the Container Service never runs a wrapup after a non-zero exit) and publishes
  `run_status FAILED` / auto QC `FAIL` in that case; `status.json` and `prereq.json` at the root
  of the tool's `/output` are the card's, not the tool's, and go verbatim to `PROVENANCE`
  instead of into the dataset;
- fetches the parent container's stdout/stderr and timing from the Container Service into
  `logs/` (found by the workflow id in `status.json`, else by the mount the two containers
  share: the Container Service resolves the wrapup's `/input` from the parent's output mount,
  and never by workflow-id order, which a concurrent run can break); `--no-publish` leaves this
  capture on and suppresses only the record;
- records where and how it ran for audit and billing (`config_json`: backend, node, reserved envelope, per-phase wall clock, total); writes `report.html` (what ran, where, how it ended, the files in `DERIVED`, links to the tool's own HTML reports where they live in `DERIVED`, log tails) and `wrapup.json`; records the orchestration (`next_step_id`, step, job id) and the prerequisites record-fetch resolved in the record's `inputs_json`; `--pointer-only` leaves only `wrapup.json` for the output handler so the record is the single owner of the data (files the record could not take because they were empty are removed too, not left for the session);
- publishes the record with the `XNW_*` contract exactly as seg-wrapup does. Resources:
  `REPORT` report.html, `PROVENANCE` wrapup.json + status.json + prereq.json, `LOGS` logs/*.log,
  `DERIVED` the tool's tree; a card names `METRICS` (or any other view) with globs relative to the
  tree (`XNW_RESOURCE_METRICS=sub-*/**/*.json`), which become a `views` mapping in `wrapup.json`
  and `results_json`, not a resource. A `raw/`-prefixed glob from a 0.5.0 card is rebased with a warning.

Environment: `PROC_PIPELINE_NAME` / `PROC_PIPELINE_VERSION` (fallback `SEG_MODEL_*`, then
`XNW_CARD_ID`/`XNW_CARD_REVISION`), `PROC_PROJECT` / `PROC_SESSION_ID` / `PROC_SCAN_ID` (or the
`SEG_*` names), `PROC_NO_PUBLISH`, `PROC_RECORD_LABEL`.
