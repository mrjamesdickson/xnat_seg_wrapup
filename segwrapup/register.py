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
    session: str
    scan: str = ""
    #: JSESSIONID from ``open_session``; empty means Basic auth per request.
    jsession: str = ""
    #: True once a login was attempted, so a failed login is not retried on every request.
    session_tried: bool = False

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "XnatContext | None":
        """Build the context from the container environment, or None with a log line saying what is missing."""
        env = os.environ if environ is None else environ
        required = {
            "XNAT_HOST": env.get("XNAT_HOST", ""),
            "XNAT_USER": env.get("XNAT_USER", ""),
            "XNAT_PASS": env.get("XNAT_PASS", ""),
            "SEG_PROJECT": env.get("SEG_PROJECT", "") or env.get("PROC_PROJECT", ""),
            "SEG_SESSION_ID": env.get("SEG_SESSION_ID", "") or env.get("PROC_SESSION_ID", ""),
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            logger.info("ROI registration skipped (and publishing); XNAT context missing %s", ", ".join(missing))
            return None
        return cls(
            host=required["XNAT_HOST"].rstrip("/"),
            user=required["XNAT_USER"],
            password=required["XNAT_PASS"],
            project=required["SEG_PROJECT"].strip(),
            session=required["SEG_SESSION_ID"].strip(),
            scan=(env.get("SEG_SCAN_ID", "") or env.get("PROC_SCAN_ID", "")).strip(),
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


def collection_label(model_name: str, scan: str, when: datetime | None = None) -> str:
    """A label OHIF will accept and a human can read: ``<model>_scan<id>_<UTC stamp>``."""
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    parts = [_LABEL_SAFE.sub("_", model_name).strip("_") or "SEG"]
    if scan:
        parts.append(f"scan{_LABEL_SAFE.sub('_', scan)}")
    parts.append(stamp)
    return "_".join(parts)[:64]


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
