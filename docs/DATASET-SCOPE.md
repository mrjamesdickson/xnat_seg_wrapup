# Dataset scope (0.7.0)

Why: James, 2026-09-24, "I want to make the group level results save like the generic subject
and session level analysis results." The group-level plugin's runs (FitLins, MRIQC group,
BIDS validator, nnU-Net) left tool-specific project assets (`analysis:groupGlmData` and five
others) written by a per-tool extractor. The registry's cards need one publisher and one record
shape at every scope, so the third scope is added here, beside session (0.3.0) and subject
(0.6.2). The card side is container-workshop `docs/DATASET-SCOPE-CARDS-DESIGN.md`.

## What a dataset-scoped run is

The environment names the frozen dataset and nothing narrower: `PROC_DATASET_ID=#INPUT_DATASET_ID#`
(or `SEG_DATASET_ID`), with `PROC_SESSION_ID` and `PROC_SUBJECT_ID` unset. The id is that of an
`analysis:analysisDatasetData` project asset (the analysis schema plugin's type) carrying a
ready file tree; the wrapper launches at `xnat:abstractProjectAsset` with a `ProjectAsset` root
named `dataset`. The wrapup does not care how the tree got there (James, 2026-09-24): the
group-level plugin freezes and materialises a cohort, or someone uploads a complete dataset to
the asset as a tree of files; neither the run nor its record needs the group-level plugin
installed. `XnatContext.scope` is then `dataset` and `XnatContext.target` the asset id.
Precedence is narrowest-wins: a run that also names a subject is subject-scoped, one that names
a session is session-scoped, so a wrapper that exposes more than one id changes nothing.

## The record

`analysis:groupAnalysisData`, a project asset (`xnat:abstractProjectAsset`): an experiment of
the project with no subject. Created by label under the project
(`PUT /data/projects/P/experiments/<label>?inbody=true`, the XML root `analysis:GroupAnalysis`
with `project` and `label` attributes and `xnat:date`, no owner element), read and deleted by
id under `/data/experiments/<id>`; role files are plain experiment resources
(`/data/experiments/<id>/resources/<ROLE>/files/...`), exactly as a subject record's. Verified
on demo02 2026-09-24 with a probe record (created, resource attached, listed by type with the
`input_dataset_id` column, deleted).

Fields: the session record's, minus `scans`, plus two the schema keeps on the group type:

- `input_dataset_id`: the asset id, the citation every group run carries and the column runs
  are listed by (the dataset's landing page, and the group-level plugin's pages where it is
  installed);
- `subject_count`: the cohort's `included_count`, read from the asset at wrapup time
  (`register.fetch_dataset_facts`); absent when the asset did not answer or does not carry one
  (an uploaded dataset has no count unless whoever made it set one), never a failed record.

`inputs_json` carries `scope: dataset`, `dataset` (id), `dataset_label`, `project` and
`included_count` (`None` when unknown), so a reader knows the cohort without opening the tool's
output. proc-wrapup reads the asset once and passes these in its `inputs`;
`publish.scope_provenance` reads them for a caller that did not (seg-wrapup, the failure record),
as it lists a subject's sessions at subject scope. The label is
`<pipeline>_<dataset label>_<stamp>_record`.

Rejected: a fourth wrapup image for group runs. One publisher is the point; the group-level
plugin's `group-analysis-wrapup` retires as its commands become cards.

## What a dataset-scoped run does not get

- **Prerequisites.** record-fetch exits 2 with the reason when a dataset run declares
  `XNW_PREREQ_*`, before it copies anything: the pass-through of `/input` comes after every
  refusal that needs nothing from it, so a large dataset is not copied only to be refused (and a
  full output cannot hide the reason). The dataset is the input and is assumed complete: which sessions carry which
  derivatives was settled when it was made (the group-level plugin's readiness check, its
  ADR-0004, or whoever uploaded the tree); the run reads the tree as it is.
  Resolving records against a dataset (a FLAME group over participant records, say) is a later
  extension of record-fetch, not a silent no-op now.
- **ROI registration.** seg-wrapup skips it, as at subject scope: the ROI collection API is per
  session.
- **A failure record.** record-fetch's failure record is published at session and subject scope
  after an unmet prerequisite; at dataset scope there are no prerequisites to be unmet.

## The pointer resource

With `PROC_POINTER_ONLY=1` the record owns the bytes and the handler's `as-a-child-of: dataset`
resource holds only `wrapup.json`: one small file on the frozen cohort, named for the card,
strictly less than the whole `OUTPUT` tree the old wrapup attached there. A site that wants
nothing written on a frozen dataset points the handler at the project instead, at the cost of
the dataset page not listing the run.

## Limits

- `subject_count` is what the asset said at wrapup time; a cohort re-frozen later is a new asset
  and a new run.
- The asset is read by id under `/data/experiments`; an id that is not an
  `analysis:analysisDatasetData` is cited anyway, with a warning, because the record must not
  depend on how a site made its dataset.
- Labels are unique per project, so two dataset runs of one pipeline finishing in the same
  second on two cohorts with the same label collide and one is relabelled with a suffix, as at
  the other scopes.
