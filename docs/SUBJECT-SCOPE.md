# Subject scope (0.6.2)

Why: plan decisions D22/D23/D26 (`xnat_genericProcessing_plugin/docs/IMPLEMENTATION-PLAN.md`).
Every BIDS App card runs at session scope and at subject scope; the subject wrapper's setup
(`xnatworks/xnat2bids-setup:2.0`) assembles every session of the subject into one BIDS tree, so
the App's output spans sessions and belongs to the subject, not to any one session.

## What a subject-scoped run is

The environment names the subject and no session: `PROC_SUBJECT_ID=#SUBJECT_ID#` (or
`SEG_SUBJECT_ID`) with `PROC_SESSION_ID` unset. `XnatContext.scope` is then `subject` and
`XnatContext.target` the subject id. A run that names both is session-scoped: the session wins,
so an existing session wrapper that happens to expose the subject is unchanged.

## The record

`analysis:subjectAnalysisData`, a subject assessor (`xnat:subjectAssessorData`), the same
fields as the session record minus `scans`. Created by label under the subject
(`PUT /data/projects/P/subjects/S/experiments/<label>?inbody=true`), read and deleted by id under
`/data/experiments/<id>`; role files are plain experiment resources
(`/data/experiments/<id>/resources/<ROLE>/files/...`), not `out` resources, because a subject
assessor is an experiment of its own. `inputs_json` carries `scope`, `subject` and `sessions`
(id and label of every image session of the subject, listed from the project-scoped
subject-experiments endpoint), so a reader knows which sessions the tree contained without
opening the tool's output. The label is `<pipeline>_<subject label>_<stamp>_record`.

Rejected alternative: publishing one session record per session of the subject. The output is
one dataset (fMRIPrep merges a subject's anatomicals across sessions), so splitting it would
either duplicate files or leave each session record incomplete.

## Prerequisites at subject scope

- A session-scoped record prerequisite (the default, e.g. the BIDS conversion) must be
  satisfied on **every** image session of the subject, because the setup assembled them all.
  The records are materialised under `prereq/<name>/<session label>/` and `prereq.json`
  lists them per session; `record` is the newest of them. One unmet session fails the gate
  and the message names which.
- A session resource prerequisite (`resource=BIDS`) is gathered from every session the same
  way. A scan resource prerequisite is refused: a subject run has no scan.
- `scope=subject` on a record prerequisite selects a subject record of the subject (xcp-d
  after a subject-scoped fMRIPrep).
- At **session** scope, own scope first: a prerequisite that no session record satisfies
  falls back to the subject's records, since a subject-level run holds `sub-X/ses-Y` for the
  session. The chosen record's `scope` is in `prereq.json`.
- A failed gate at subject scope publishes the FAILED record on the subject.

## The records mount for a subject wrapper

A setup command needs a mount with an XNAT source, and a subject has no directory. The
subject wrapper derives a second Project input from the subject and attaches
`record-fetch --no-passthrough` to it (the project archive is bind-mounted read-only, nothing
is copied): the setup writes only `prereq/` and `prereq.json` into the mount.

## Limits

- The subject record's session list is what XNAT holds at wrapup time; a session added later is
  not in it.
- `list_records` filters the project-wide listing client-side; a project with tens of
  thousands of records pays that listing on every launch (same as session scope today).
- The subject-record fallback at session scope picks the newest satisfying subject record;
  it does not check that the record's session list contains this session.
