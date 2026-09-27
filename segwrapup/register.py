"""Register a DICOM SEG with XNAT as an ROI collection so OHIF lists it.

The wrapup receives the launch context because the Container Service copies the
parent command's resolved environment onto wrapup containers: the parent sets
``SEG_PROJECT``/``SEG_SESSION_ID``/``SEG_SCAN_ID`` from its derived inputs, and CS
itself injects ``XNAT_HOST``/``XNAT_USER``/``XNAT_PASS`` (an alias token).

The call is the one the OHIF viewer plugin's ROI API expects and the same one the
older TotalSegmentator container makes for RTStruct::

    PUT {XNAT_HOST}/xapi/roi/projects/{project}/sessions/{session}/collections/{label}?type=SEG&overwrite=true
"""
from __future__ import annotations

import base64
import http.client
import logging
import os
import re
import urllib.error
import urllib.parse
import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LABEL_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class XnatContext:
    host: str
    user: str
    password: str
    project: str
    #: The session the run belongs to (session scope), or empty at subject scope.
    session: str
    scan: str = ""
    #: The subject the run belongs to at subject scope (a subject-context launch whose BIDS
    #: tree spans every session of the subject); records are then subject assessors.
    subject: str = ""
    #: The frozen dataset (an ``analysis:analysisDatasetData`` project asset) the run belongs to
    #: at dataset scope (a dataset-context launch on a ready tree, however it was made); records
    #: are then ``analysis:groupAnalysisData`` project assets of the project (0.7.0).
    dataset: str = ""
    #: JSESSIONID from ``open_session``; empty means Basic auth per request.
    jsession: str = ""
    #: True once a login was attempted, so a failed login is not retried on every request.
    session_tried: bool = False

    @property
    def scope(self) -> str:
        """``session``, ``subject`` or ``dataset``: what the run's records hang from. A session
        wins over a subject, a subject over a dataset, so a wrapper that exposes more than one
        id keeps the narrowest scope."""
        if self.session:
            return "session"
        if self.subject:
            return "subject"
        return "dataset" if self.dataset else "session"

    @property
    def target(self) -> str:
        """The XNAT id the run belongs to: the session, the subject at subject scope, or the
        dataset (project asset) at dataset scope."""
        return self.session or self.subject or self.dataset

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "XnatContext | None":
        """Build the context from the container environment, or None with a log line saying what is missing.

        A run is session-scoped (``SEG_SESSION_ID`` / ``PROC_SESSION_ID``), subject-scoped
        (``SEG_SUBJECT_ID`` / ``PROC_SUBJECT_ID``, no session id) or dataset-scoped
        (``PROC_DATASET_ID`` / ``SEG_DATASET_ID``, neither of the others): a subject- or
        dataset-context wrapper sets its own variable and leaves the narrower ones unset."""
        env = os.environ if environ is None else environ
        required = {
            "XNAT_HOST": env.get("XNAT_HOST", ""),
            "XNAT_USER": env.get("XNAT_USER", ""),
            "XNAT_PASS": env.get("XNAT_PASS", ""),
            "SEG_PROJECT": env.get("SEG_PROJECT", "") or env.get("PROC_PROJECT", ""),
        }
        session = (env.get("SEG_SESSION_ID", "") or env.get("PROC_SESSION_ID", "")).strip()
        subject = (env.get("SEG_SUBJECT_ID", "") or env.get("PROC_SUBJECT_ID", "")).strip()
        dataset = (env.get("SEG_DATASET_ID", "") or env.get("PROC_DATASET_ID", "")).strip()
        missing = [name for name, value in required.items() if not value.strip()]
        if not session and not subject and not dataset:
            missing.append("SEG_SESSION_ID (or SEG_SUBJECT_ID for a subject-scoped run, PROC_DATASET_ID for a dataset-scoped run)")
        if missing:
            logger.info("ROI registration skipped (and publishing); XNAT context missing %s", ", ".join(missing))
            return None
        return cls(
            host=required["XNAT_HOST"].rstrip("/"),
            user=required["XNAT_USER"],
            password=required["XNAT_PASS"],
            project=required["SEG_PROJECT"].strip(),
            session=session,
            scan=(env.get("SEG_SCAN_ID", "") or env.get("PROC_SCAN_ID", "")).strip(),
            subject=subject,
            dataset=dataset,
        )


def auth_headers(context: XnatContext) -> dict[str, str]:
    """The auth header for one request: the run's session cookie when ``open_session`` worked,
    otherwise Basic auth. Never both: with a valid cookie XNAT reuses the session, and a Basic
    header on top would only invite a second one."""
    if not context.jsession and not context.session_tried:
        open_session(context)          # lazily: a run that makes no request never logs in
    if context.jsession:
        return {"Cookie": f"JSESSIONID={context.jsession}"}
    credentials = base64.b64encode(f"{context.user}:{context.password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def open_session(context: XnatContext, timeout_seconds: float = 60.0) -> bool:
    """Log in once for the whole run (``POST /data/JSESSION``) so the dozen requests a wrapup
    makes share one XNAT session instead of leaving one lingering session each. On any failure
    the run continues with Basic auth per request; publishing never depends on this."""
    object.__setattr__(context, "session_tried", True)
    credentials = base64.b64encode(f"{context.user}:{context.password}".encode()).decode()
    request = urllib.request.Request(f"{context.host}/data/JSESSION", method="POST",
                                     headers={"Authorization": f"Basic {credentials}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            token = response.read().decode(errors="replace").strip()
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as error:
        logger.warning("could not open an XNAT session (%s); falling back to Basic auth per request", error)
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", token):
        logger.warning("POST /data/JSESSION answered something that is not a session id; falling back to Basic auth per request")
        return False
    object.__setattr__(context, "jsession", token)
    logger.info("XNAT session opened for %s", context.user)
    return True


def close_session(context: XnatContext, timeout_seconds: float = 60.0) -> None:
    """Log the run's session out (``DELETE /data/JSESSION``). Best effort: a failure is logged,
    the session then expires on the server's idle timeout."""
    if not context.jsession:
        return
    request = urllib.request.Request(f"{context.host}/data/JSESSION", method="DELETE", headers=auth_headers(context))
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds):
            pass
        logger.info("XNAT session closed")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as error:
        logger.warning("could not close the XNAT session (it will expire on its own): %s", error)
    finally:
        object.__setattr__(context, "jsession", "")


LABEL_MAX = 64


def collection_label(model_name: str, scan: str, when: datetime | None = None, session_label: str = "", reserve: int = 0) -> str:
    """A label OHIF will accept and a human can read:
    ``<model>_<session label>_scan<id>_<UTC stamp>``.

    XNAT experiment labels are unique per *project*, not per session, so the session label is
    part of it: a batch that finishes two runs of one pipeline in the same second (Merlin on
    RSNA0001/RSNA0002, 2026-09-06) otherwise builds the same label twice and the second create
    is refused with 409. When the whole thing exceeds ``LABEL_MAX`` the model name is trimmed,
    never the session, scan or stamp that make it unique. ``reserve`` leaves room for a suffix the
    caller appends (proc-wrapup's ``_record``): a 64-character label plus ``_record`` is 71 and XNAT
    refuses the record (Codex P2, PR #21)."""
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    limit = max(LABEL_MAX - max(reserve, 0), 1)
    scan_part = f"scan{_LABEL_SAFE.sub('_', scan)}" if scan else ""
    owner = _LABEL_SAFE.sub("_", session_label).strip("_") if session_label else ""
    model = _LABEL_SAFE.sub("_", model_name).strip("_") or "SEG"
    # what must survive: the stamp and the scan (uniqueness); then the owner, then the model.
    # A long dataset label used to push the stamp off the end (proc-wrapup dataset scope).
    # "M_" plus the scan and stamp must fit the limit; a very long scan id gives up its tail before
    # the stamp does, and the model keeps at least its one letter (Codex P2, PR #21 round 15)
    if scan_part and len(scan_part) + 1 + len(stamp) + 2 > limit:
        scan_part = scan_part[:max(limit - len(stamp) - 3, 0)].rstrip("_")
    fixed = "_".join(part for part in (scan_part, stamp) if part)
    room = limit - len(fixed) - 1                         # for "<model>_" at least
    if owner:
        owner = owner[:max(room - 2, 0)].rstrip("_")     # leave "M_" for the model
    room = limit - len("_".join(part for part in (owner, fixed) if part)) - 1
    model = model[:max(room, 1)].rstrip("_") or "M"
    label = "_".join(part for part in (model, owner, fixed) if part)
    return label[:limit]


def _encodable(value: str, what: str) -> str:
    """``value`` with anything that cannot be encoded as UTF-8 written out as its escape. XNAT can
    answer a JSON string holding a lone surrogate (``"\ud800"``, which the group-level plugin would
    have to have stored, but the reader cannot assume it did not): Python decodes it happily and then
    every *write* of it raises UnicodeEncodeError — the HTML report, the record XML, ``wrapup.json`` —
    after the run has finished and before the record is published, so a whole run would lose its record
    over a label (Codex P2, PR #21 round 33). These readers are best effort by contract, so the label
    comes back readable and diagnosable (``\\ud800``) rather than unwritable. The offending value is
    never logged: writing it to a log stream raises the same error."""
    try:
        value.encode()
        return value
    except UnicodeEncodeError:
        cleaned = value.encode(errors="backslashreplace").decode()
        logger.warning("the %s cannot be encoded as UTF-8 (it holds an unpaired surrogate); using %r", what, cleaned)
        return cleaned


def fetch_target_label(context: XnatContext, timeout_seconds: float = 60.0) -> str:
    """The label of what the run belongs to: the session's, the subject's at subject scope, the
    dataset's at dataset scope."""
    if context.scope == "subject":
        return fetch_subject_label(context, timeout_seconds)
    if context.scope == "dataset":
        return fetch_dataset_label(context, timeout_seconds)
    return fetch_session_label(context, timeout_seconds)


def fetch_dataset_facts(context: XnatContext, timeout_seconds: float = 60.0) -> dict:
    """What the record says about the cohort it ran on: the dataset's ``label`` and its
    ``included_count`` (the ``analysis:analysisDatasetData`` member count; the group-level plugin
    sets it when it freezes a cohort, an uploaded dataset may not carry one), read from ``/data/experiments/<asset id>``. Empty with a
    warning when XNAT does not answer or the id is not a dataset: the record is still published,
    it names the id and no count."""
    if not context.dataset:
        return {}
    url = f"{context.host}/data/experiments/{urllib.parse.quote(context.dataset, safe='')}?format=json"
    request = urllib.request.Request(url, headers=auth_headers(context))
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode())
        item = payload["items"][0]
        fields = item["data_fields"]
        if not isinstance(item, dict) or not isinstance(fields, dict):
            # valid JSON of the wrong shape (data_fields null or a list) is as unreadable as no
            # answer; caught here, not as an AttributeError below (Codex P2, PR #21 round 10)
            raise TypeError(f"data_fields is {type(fields).__name__}, not an object")
        # XNAT puts the type in the item's ``meta``, not among the data fields; a meta of the wrong
        # shape only costs the type check, still inside the guarded parse (Codex P2, round 11)
        meta = item.get("meta")
        if meta is not None and not isinstance(meta, dict):
            logger.warning("dataset %s answered a meta of type %s, not an object; its type is not checked", context.dataset, type(meta).__name__)
            meta = {}
        xsi = str((meta or {}).get("xsi:type") or "")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError,
            RecursionError) as error:
        # HTTPException: a truncated body (IncompleteRead) is not an OSError (Codex P2, PR #21);
        # RecursionError: valid JSON nested past the parser's limit, before the record is published (round 27)
        logger.warning("could not read dataset %s (%s); the record names the id and no member count", context.dataset, error)
        return {}
    facts = {"label": _encodable(str(fields.get("label") or "").strip(), f"label of dataset {context.dataset}")}
    count = fields.get("included_count")
    if count not in (None, ""):
        try:
            facts["included_count"] = _whole_count(count)
        except (TypeError, ValueError, OverflowError):
            # 1e309 decodes as infinity and int() of it raises OverflowError; 2.5 is no member count
            # either. Best effort: the record names the id and no count (Codex P2, PR #21 round 14)
            logger.warning("dataset %s carries a non-numeric included_count %r; not recorded", context.dataset, count)
    if xsi and xsi != "analysis:analysisDatasetData":
        logger.warning("dataset %s is a %s, not an analysis:analysisDatasetData; the record still cites it", context.dataset, xsi)
    return facts


#: ``subject_count`` is an xs:integer stored as a 32-bit column; a larger literal makes XNAT refuse
#: the otherwise valid record, so the best-effort reader leaves it off (Codex P2, PR #21 round 16).
COUNT_MAX = 2_147_483_647


def _whole_count(value) -> int:
    """``included_count`` as a non-negative whole number; raises for anything else (bool, a
    non-integral or non-finite float, text that is not an integer)."""
    if isinstance(value, bool):
        raise TypeError("a boolean is not a count")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{value!r} is not a whole number")
        value = int(value)
    if isinstance(value, str) and len(value.strip()) > 12:
        raise ValueError("too many digits for a member count")   # before int(): a 400-digit literal is not a count
    count = int(value)
    if count < 0:
        raise ValueError(f"{count} is negative")
    if count > COUNT_MAX:
        raise ValueError(f"{count} exceeds the schema's integer range")
    return count


def fetch_dataset_label(context: XnatContext, timeout_seconds: float = 60.0) -> str:
    """The dataset's label for ``context.dataset``; the id when XNAT does not answer."""
    return fetch_dataset_facts(context, timeout_seconds).get("label") or context.dataset


def fetch_subject_label(context: XnatContext, timeout_seconds: float = 60.0) -> str:
    """The subject's label for ``context.subject`` (``XNAT_S09007`` -> ``292``); the id when XNAT
    does not answer, so the record label is still unique."""
    url = (f"{context.host}/data/projects/{urllib.parse.quote(context.project, safe='')}/subjects/"
           f"{urllib.parse.quote(context.subject, safe='')}?format=json")
    request = urllib.request.Request(url, headers=auth_headers(context))
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode())
        label = _encodable(str(payload["items"][0]["data_fields"].get("label") or "").strip(),
                           f"label of subject {context.subject}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as error:
        logger.warning("could not read the label of subject %s (%s); the record label carries the id instead", context.subject, error)
        return context.subject
    return label or context.subject


def list_subject_sessions(context: XnatContext, subject: str, timeout_seconds: float = 60.0) -> list[dict]:
    """``[{"ID", "label"}]`` of the subject's image sessions, by label. Project-scoped: the
    site-wide ``/data/subjects/<id>/experiments`` returns the subject document, not rows.
    Raises (URLError/OSError/ValueError) when XNAT does not answer; the caller decides what a
    record without its session list means."""
    url = (f"{context.host}/data/projects/{urllib.parse.quote(context.project, safe='')}/subjects/"
           f"{urllib.parse.quote(subject, safe='')}/experiments?format=json&columns=ID,label,xsiType")
    request = urllib.request.Request(url, headers=auth_headers(context))
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode())
    rows = payload.get("ResultSet", {}).get("Result", []) if isinstance(payload, dict) else []
    sessions = [{"ID": r.get("ID"), "label": r.get("label") or r.get("ID")} for r in rows
                if r.get("ID") and "SessionData" in str(r.get("xsiType") or "SessionData")]
    return sorted(sessions, key=lambda s: s["label"])


def fetch_session_label(context: XnatContext, timeout_seconds: float = 60.0) -> str:
    """The session's label (``RSNA0002``) for ``context.session`` (``XNAT_E25251``); empty when
    XNAT does not answer, so callers fall back to the id and still get a unique label."""
    url = f"{context.host}/data/experiments/{urllib.parse.quote(context.session, safe='')}?format=json"
    request = urllib.request.Request(url, headers=auth_headers(context))
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode())
        label = _encodable(str(payload["items"][0]["data_fields"].get("label") or "").strip(),
                           f"label of session {context.session}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as error:
        logger.warning("could not read the label of session %s (%s); the record label carries the id instead", context.session, error)
        return context.session
    return label or context.session


def register_roi_collection(
    context: XnatContext,
    seg_path: Path,
    label: str,
    collection_type: str = "SEG",
    timeout_seconds: float = 300.0,
) -> dict:
    """PUT the file as an ROI collection. Raises RuntimeError with the HTTP detail on failure."""
    url = (
        f"{context.host}/xapi/roi/projects/{urllib.parse.quote(context.project, safe='')}"
        f"/sessions/{urllib.parse.quote(context.session, safe='')}"
        f"/collections/{urllib.parse.quote(label, safe='')}"
        f"?type={collection_type}&overwrite=true"
    )
    body = seg_path.read_bytes()
    request = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={**auth_headers(context), "Content-Type": "application/octet-stream"},
    )
    logger.info("registering %s (%d bytes) as %s collection %s", seg_path.name, len(body), collection_type, label)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = response.status
            text = response.read().decode(errors="replace")[:500]
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise RuntimeError(f"ROI collection PUT {url} failed: HTTP {error.code} {detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"ROI collection PUT {url} failed: {error}") from error
    logger.info("ROI collection %s registered: HTTP %d", label, status)
    return {"label": label, "type": collection_type, "status": status, "response": text, "url": url.split("?")[0]}
