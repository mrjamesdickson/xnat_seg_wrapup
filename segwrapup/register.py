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
    #: JSESSIONID from ``open_session``; empty means Basic auth per request.
    jsession: str = ""
    #: True once a login was attempted, so a failed login is not retried on every request.
    session_tried: bool = False

    @property
    def scope(self) -> str:
        """``session`` or ``subject``: what the run's records hang from."""
        return "subject" if self.subject and not self.session else "session"

    @property
    def target(self) -> str:
        """The XNAT id the run belongs to: the session, or the subject at subject scope."""
        return self.session or self.subject

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "XnatContext | None":
        """Build the context from the container environment, or None with a log line saying what is missing.

        A run is session-scoped (``SEG_SESSION_ID`` / ``PROC_SESSION_ID``) or subject-scoped
        (``SEG_SUBJECT_ID`` / ``PROC_SUBJECT_ID``, no session id): a subject-context wrapper
        sets the subject variable and leaves the session one unset."""
        env = os.environ if environ is None else environ
        required = {
            "XNAT_HOST": env.get("XNAT_HOST", ""),
            "XNAT_USER": env.get("XNAT_USER", ""),
            "XNAT_PASS": env.get("XNAT_PASS", ""),
            "SEG_PROJECT": env.get("SEG_PROJECT", "") or env.get("PROC_PROJECT", ""),
        }
        session = (env.get("SEG_SESSION_ID", "") or env.get("PROC_SESSION_ID", "")).strip()
        subject = (env.get("SEG_SUBJECT_ID", "") or env.get("PROC_SUBJECT_ID", "")).strip()
        missing = [name for name, value in required.items() if not value.strip()]
        if not session and not subject:
            missing.append("SEG_SESSION_ID (or SEG_SUBJECT_ID for a subject-scoped run)")
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


def collection_label(model_name: str, scan: str, when: datetime | None = None, session_label: str = "") -> str:
    """A label OHIF will accept and a human can read:
    ``<model>_<session label>_scan<id>_<UTC stamp>``.

    XNAT experiment labels are unique per *project*, not per session, so the session label is
    part of it: a batch that finishes two runs of one pipeline in the same second (Merlin on
    RSNA0001/RSNA0002, 2026-09-06) otherwise builds the same label twice and the second create
    is refused with 409. When the whole thing exceeds ``LABEL_MAX`` the model name is trimmed,
    never the session, scan or stamp that make it unique."""
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    tail = []
    if session_label:
        tail.append(_LABEL_SAFE.sub("_", session_label).strip("_"))
    if scan:
        tail.append(f"scan{_LABEL_SAFE.sub('_', scan)}")
    tail.append(stamp)
    suffix = "_".join(part for part in tail if part)
    model = _LABEL_SAFE.sub("_", model_name).strip("_") or "SEG"
    model = model[:max(LABEL_MAX - len(suffix) - 1, 1)].rstrip("_") or "SEG"
    return f"{model}_{suffix}"[:LABEL_MAX]


def fetch_target_label(context: XnatContext, timeout_seconds: float = 60.0) -> str:
    """The label of what the run belongs to: the session's, or the subject's at subject scope."""
    if context.scope == "subject":
        return fetch_subject_label(context, timeout_seconds)
    return fetch_session_label(context, timeout_seconds)


def fetch_subject_label(context: XnatContext, timeout_seconds: float = 60.0) -> str:
    """The subject's label for ``context.subject`` (``XNAT_S09007`` -> ``292``); the id when XNAT
    does not answer, so the record label is still unique."""
    url = (f"{context.host}/data/projects/{urllib.parse.quote(context.project, safe='')}/subjects/"
           f"{urllib.parse.quote(context.subject, safe='')}?format=json")
    request = urllib.request.Request(url, headers=auth_headers(context))
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode())
        label = str(payload["items"][0]["data_fields"].get("label") or "").strip()
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
        label = str(payload["items"][0]["data_fields"].get("label") or "").strip()
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
