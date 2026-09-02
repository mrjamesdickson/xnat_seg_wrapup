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

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "XnatContext | None":
        """Build the context from the container environment, or None with a log line saying what is missing."""
        env = os.environ if environ is None else environ
        required = {
            "XNAT_HOST": env.get("XNAT_HOST", ""),
            "XNAT_USER": env.get("XNAT_USER", ""),
            "XNAT_PASS": env.get("XNAT_PASS", ""),
            "SEG_PROJECT": env.get("SEG_PROJECT", ""),
            "SEG_SESSION_ID": env.get("SEG_SESSION_ID", ""),
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            logger.info("ROI registration skipped; missing %s", ", ".join(missing))
            return None
        return cls(
            host=required["XNAT_HOST"].rstrip("/"),
            user=required["XNAT_USER"],
            password=required["XNAT_PASS"],
            project=required["SEG_PROJECT"].strip(),
            session=required["SEG_SESSION_ID"].strip(),
            scan=env.get("SEG_SCAN_ID", "").strip(),
        )


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
    credentials = base64.b64encode(f"{context.user}:{context.password}".encode()).decode()
    body = seg_path.read_bytes()
    request = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/octet-stream"},
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
