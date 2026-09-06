# Changelog

## 0.4.0 (2026-09-06)

The generic-assessor strategy for every card (James, 2026-09-06):

- **proc-wrapup**, a second entrypoint and image (`xnatworks/proc-wrapup`, built from this package): the generic wrapup for any card. Keeps everything the tool wrote under `raw/`, captures the run's execution state (`status.json` from a trapped failure; the parent container's stdout/stderr and timing from the Container Service, into `logs/`), writes a report that interprets nothing, and publishes the `analysis:sessionAnalysisData` record with `LOGS`, `REPORT`, `PROVENANCE` and `DERIVED` resources (plus `METRICS` where the card names globs). `run_status FAILED` / auto QC `FAIL` when the parent recorded a non-zero exit.
- seg-wrapup also keeps the tool's other output verbatim under `raw/` (statistics, label files, logs were dropped before).
- One XNAT session per run: login once on the first request, cookie on every call, logout at the end; Basic auth fallback.
- Record fields `container_id` and `duration_seconds` are filled when the Container Service knows them; the parent container is found by the shared build mount (`xnat-host-path`), never by workflow-id adjacency; `duration_seconds` spans the docker `running` to `complete` events. A malformed log response is logged and skipped, not fatal. The record's `wrapup_version` names the wrapup that published it (`proc-wrapup 0.4.0` or `seg-wrapup 0.4.0`).
- `PROC_*` environment names are accepted alongside `SEG_*` for the XNAT context.

## 0.3.1 (2026-09-06)

Hardening of the analysis-record publisher from the PR #3 review, no new behaviour:

- create-only enforced: the label is probed first (proceeds only on 404) and a failed file upload deletes the new record again
- `auto_qc_status` is WARN when any delivered mask could not be measured
- overlapping role globs assign each file once; resource roles validated at parse time; upload `format` sanitised and URL-encoded
- every post-create failure (OSError, urllib InvalidURL, malformed HTTP responses, truncated error bodies) is recorded in `wrapup.json`, never fatal
- README documents the discrete `XNW_*` variables and the `DERIVED` role

