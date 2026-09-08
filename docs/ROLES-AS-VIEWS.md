# Roles are views onto the output tree (0.6.0)

Plan decision D20 (`xnat_genericProcessing_plugin/docs/IMPLEMENTATION-PLAN.md`), taken by James
on 2026-09-08 while looking at the first BIDS records on demo02:

> "raw/sub-H025.html should be in with everything else." … "logs good, report standard did it
> run report good, provenance good, DERIVED should contain the entire output of the container
> as we expect." … "why are we copying files outside derived" … "we have a predefined dataset
> that's created by the scientists. Don't fuck it up."

This note is the technical record: what the publisher does now, why, what was rejected, and
the traps. The user-facing description is in the README ("Analysis record").

## The defect

Until 0.5.0 `collect_files` gave every file to exactly one role: the card's `METRICS` and
`REPORT` globs were matched first, and `DERIVED` took "everything else". A record's resources
were a partition of the output, which looked complete (nothing was lost) but was not a
dataset:

- mriqc E25614: 8 reports in `REPORT`, 16 IQM JSONs in `METRICS`, 49 other files in `DERIVED`.
  A consumer of `role=DERIVED` got mriqc's SVGs and logs and none of its results.
- fmriprep E25617: `raw/sub-H025.html` alone in `REPORT`, where it rendered without its
  figures (relative links into `sub-H025/figures/`, which were in `DERIVED`); 721 files in
  `DERIVED`. fmriprep 0.1.0's `METRICS` had carved the confounds out of `DERIVED`, so xcp-d and
  giga-connectome would have received BOLD without confounds (Codex P1 on workshop #15, worked
  around there by dropping fmriprep's `METRICS`; inventory gap G9).
- The card's exit trap writes `status.json` into the tool's `/output`, so it appeared inside
  the dataset (`METRICS raw/status.json` on E25614).
- Everything sat under a `raw/` prefix on the record, so every consumer wrote `.../raw` into
  its paths and the BIDS dataset was not at the root of anything.

D16 says a record *is* a derivative in the BIDS sense and `DERIVED` *is* the derivatives
dataset. A dataset with pieces moved out and a `raw/` wrapper is not that.

## The rule

1. **`DERIVED` is the tool's complete output tree at the resource root**: byte-for-byte,
   path-for-path, nothing renamed, moved, filtered or reformatted. Hidden entries are the one
   exclusion (`.source_dicom` is the DICOM XNAT already holds; documented since 0.1).
2. **The record has four resources and only four.** `REPORT`, `PROVENANCE` and `LOGS` hold what
   the wrapup itself generated; a tool file is never copied into them and a wrapup file never
   lands in `DERIVED`. A card's `XNW_RESOURCE_<fixed role>` is ignored, logged and listed in
   `wrapup.json` under `ignored_overrides`.
3. **Every other role is a view**: the DERIVED paths its globs matched, recorded in
   `wrapup.json` (`"views": {"METRICS": [...]}`) and in the record's `results_json` (the
   `views` key), so a consumer resolves the role to files without a second copy. Globs are
   relative to the DERIVED root. A declared view that matched nothing is recorded empty, so
   the miss is visible on the record.
4. **Files the card's command line put into the tool's `/output`** — `status.json` from the
   exit trap, `prereq.json` copied from record-fetch — are lifted out of the tree, verbatim,
   into `PROVENANCE`. Only those two root files; anything else in `/output` is the tool's.
5. **record-fetch** resolves `role=<fixed>` as a resource and `role=<view>` through the
   record's `PROVENANCE/wrapup.json`, copying exactly the DERIVED files the view names at their
   DERIVED paths. A record with no `views` (published before 0.6.0) serves its resource of that
   name as before.

## Where the tree lives locally, and why the record differs

proc-wrapup still copies the tool's tree to `/output/raw/` and writes its own files at
`/output`. The two cannot share one directory: a tool that writes `report.html`, `logs/` or
`wrapup.json` (previous-run leftovers are real, see the pointer-only test) would collide with
the wrapup's files. The prefix is a local necessity, so `collect_files` takes a
`derived_root` and names DERIVED files relative to it. The same local layout is what the
Container Service output handler uploads to the session when a card does not use
`--pointer-only`, so a session resource written by proc-wrapup still shows `raw/…`; the record
does not. This is the one place the two disagree, and it is deliberate: the session resource
is a courtesy copy for viewers, the record is the dataset.

seg-wrapup has no `derived_root`. Its DERIVED root is the wrapup's own product (the merged or
delivered masks, their sidecars and measurements), and the tool's other output keeps its
`raw/` prefix there, because seg-wrapup *does* transform the tool's output (merging masks,
renumbering to 8-bit) and a tool file named like a wrapup product would collide at the root.
Segmentation records therefore change in one way only: `volumes.json`, `volumes.csv` and
`segmentation.tsv` moved from a `METRICS` resource into `DERIVED`, and `METRICS` became the
view that names them. Nothing implemented reads the old `METRICS` resource (checked: the
schema plugin's record and Processing pages list resources generically; workbench #90 is an
issue, not code, and its read paths — DERIVED NIfTI, `segmentation.tsv`, `labels.ctbl` — all
still exist, `segmentation.tsv` now under `DERIVED`; OHIF reads ROI collections, not records;
`batch_run_cards.py`/`collect_evidence.py` read record fields and file counts).

## Rejected alternatives

- **Copies: DERIVED whole, REPORT/METRICS as copies of their matches.** The first cut of this
  change. Rejected by James ("why are we copying files outside derived"): every copy is a
  second place a file can be stale, and a tool's HTML report copied alone into REPORT is
  broken anyway (its figures are relative links into the tree). The report page now links
  the tool's reports where they are.
- **Keep the `raw/` prefix on the record** for symmetry with the local layout. Rejected: the
  dataset must be at the root of the resource; the prefix exists only to keep the wrapup's
  files apart locally. Looked for a hard reason to keep it (a collision inside DERIVED) and
  found none: DERIVED contains only tool files, so nothing of the wrapup's can collide there.
- **Honour `XNW_RESOURCE_DERIVED` as a carve-out** (0.5.0, with Codex's P1 keeping the
  excluded files on the session under `--pointer-only`). Rejected: "DERIVED must be exactly
  that dataset" admits no card-side subsetting. The override is ignored and logged; the
  pointer-only safety net (a file not on the record stays in the output) is kept as a net,
  not as a feature.
- **Refuse a fixed-role override as a contract error.** Would have stopped every card
  currently pinned to 0.5.0 from publishing until re-pinned. Ignoring with a warning keeps
  records flowing and leaves an audit trail in `wrapup.json`.
- **Views only in `results_json`, not in `wrapup.json`.** `results_json` is capped at 65 536
  characters and already carries the DERIVED file list (721 names for fmriprep), so it can
  truncate; `wrapup.json` on PROVENANCE has no cap and is the canonical copy record-fetch
  reads. Both are written; the record field is for the page and for searches.

## Traps

- `results_json` size: a view listing hundreds of paths plus the DERIVED file list can exceed
  the field's 65 536 characters. Not hit yet (fmriprep 721 files published fine); if it is,
  the fix is to drop the per-role `files` list from `results_json`, not the views.
- The uploaded `PROVENANCE/wrapup.json` predates the publish outcome by design (it cannot
  contain the id of the record it is being uploaded to), but it does contain the views:
  `publish_if_possible` writes them into the manifest before the upload.
- `output_paths` in the publish outcome (local paths of what was uploaded and what was
  skipped as empty) exists for the pointer reduction only and is popped before the manifest
  is written, so `wrapup.json` does not list every file twice.
- A card pinned to 0.5.0 keeps working on 0.6.0: fixed-role overrides are ignored and logged,
  `raw/…` view globs are rebased and logged. But a *consumer* card that reads a prerequisite at
  `prereq/<name>/raw/…` breaks the moment its producer re-pins, because the producer's DERIVED
  no longer has the segment. Re-pin producers and consumers together (qsirecon, xcp-d,
  fmripost-aroma, giga-connectome, bidsmreye at least).
- Zero-byte files: XNAT refuses them in-body, they are skipped (`skipped_empty`) and, under
  `--pointer-only`, deleted from the output as well. An empty file's only trace is its name in
  `wrapup.json` (`raw_files`, `skipped_empty`).

## Known limits

- A tool that writes into its own root a file named `status.json` or `prereq.json` loses it
  from the dataset (it goes to PROVENANCE). No BIDS App does; the trap file names are ours.
- The record page renders `REPORT/report.html` in a sandboxed iframe with a `<base>` at
  `REPORT/files/`; the links to the tool's reports (`../../DERIVED/files/<path>`) depend on
  that base and on XNAT serving nested resource paths, which it does.
- Views name files, not directories; a role whose meaning is "this subtree" is expressed as
  a glob over it (`sub-*/func/**`).
