# Changelog

## 0.3.1 (2026-09-06)

Hardening of the analysis-record publisher from the PR #3 review, no new behaviour:

- create-only enforced: the label is probed first (proceeds only on 404) and a failed file upload deletes the new record again
- `auto_qc_status` is WARN when any delivered mask could not be measured
- overlapping role globs assign each file once; resource roles validated at parse time; upload `format` sanitised and URL-encoded
- every post-create failure (OSError, urllib InvalidURL, malformed HTTP responses, truncated error bodies) is recorded in `wrapup.json`, never fatal
- README documents the discrete `XNW_*` variables and the `DERIVED` role

