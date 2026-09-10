"""The card copy on every record (plan D27): the certificate of the run.

James, 2026-09-10: "In the output for a container we also need a copy of the card so people
can see where it came from and references etc. The certificate of the thing." The adopt tool
puts the card's ``metadata.json`` (plus the image digest and links to the README and LICENSE)
into the Container Service command as ``command-metadata.card``, plain JSON in a jsonb column
(an env var was rejected: the CS stores env values in a 255-character column). The wrapup reads
its command back from the Container Service and writes the block onto the record as
``PROVENANCE/card/metadata.json``, beside ``card/card.json`` with what only the run knew
(command and wrapper ids, the wrapup and its version, the scope, when).

The command is found through the parent container: proc-wrapup and seg-wrapup already locate
it (``execution.find_parent_container``) and it carries ``command-id``; record-fetch, a setup,
finds the main container of its own workflow. When the command cannot be read the record gets
``card.json`` alone with the reason in ``error``: the record and the dataset matter more than
the certificate, so this never fails a run.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import logging
import urllib.error
from pathlib import Path

from . import __version__
from .execution import HELPER_SUBTYPES, _get_json
from .register import XnatContext

logger = logging.getLogger("segwrapup.card")

CARD_DIRNAME = "card"
CARD_METADATA = "metadata.json"
CARD_SUMMARY = "card.json"
#: The key inside the command's ``command-metadata`` the adopt tool writes the card to.
METADATA_KEY = "card"
#: The glob the PROVENANCE defaults carry for the copy (``Path.glob``: every file under card/).
CARD_GLOB = f"{CARD_DIRNAME}/**/*"


def fetch_command_card(context: XnatContext, command_id, timeout: float = 60.0) -> tuple[dict | None, str]:
    """``command-metadata.card`` of Container Service command ``command_id``, and the reason when
    there is none ("" on success)."""
    if not command_id:
        return None, "no command id"
    url = f"{context.host}/xapi/commands/{command_id}"
    try:
        payload = _get_json(context, url, timeout)
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as error:
        logger.warning("could not read command %s for the card copy: %s", command_id, error)
        return None, f"could not read command {command_id}: {error}"
    metadata = payload.get("command-metadata") if isinstance(payload, dict) else None
    card = metadata.get(METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(card, dict) or not card:
        logger.info("command %s (%s) carries no command-metadata.%s; the record gets card.json only",
                    command_id, (payload or {}).get("name", "?") if isinstance(payload, dict) else "?", METADATA_KEY)
        return None, f"command {command_id} carries no command-metadata.{METADATA_KEY}"
    return card, ""


def locate_main_container(context: XnatContext, workflow_id: str | None, timeout: float = 60.0) -> dict | None:
    """The main (non-helper) container of ``workflow_id``: how a setup command finds the run it
    prepares. Every container of a launch shares the workflow id (main, setup, wrapup)."""
    if not workflow_id:
        return None
    try:
        containers = _get_json(context, f"{context.host}/xapi/containers", timeout)
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as error:
        logger.warning("could not list containers to find the main run of workflow %s: %s", workflow_id, error)
        return None
    mains = [c for c in containers if isinstance(c, dict) and str(c.get("workflow-id")) == str(workflow_id)
             and c.get("subtype") not in HELPER_SUBTYPES] if isinstance(containers, list) else []
    if len(mains) != 1:
        logger.info("workflow %s has %d main container(s); the card copy needs exactly one", workflow_id, len(mains))
        return None
    return mains[0]


def write_card_copy(output_dir: Path, wrapup: str, card: dict | None, error: str = "", command_id=None, wrapper_id=None,
                    scope: str = "", extra: dict | None = None) -> dict:
    """Write ``card/metadata.json`` (the block, when the command carries one) and ``card/card.json``
    under ``output_dir``; return the summary that went into ``card.json``. Never raises."""
    summary = {
        "wrapup": wrapup, "wrapup_version": __version__,
        "command_id": command_id, "wrapper_id": wrapper_id, "run_scope": scope,
        "card_id": (card or {}).get("id", ""), "card_revision": (card or {}).get("version", ""),
        "card_url": (card or {}).get("url", ""),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        **({"error": error} if error else {}),
        **(extra or {}),
    }
    card_dir = output_dir / CARD_DIRNAME
    try:
        card_dir.mkdir(parents=True, exist_ok=True)
        if card:
            (card_dir / CARD_METADATA).write_text(json.dumps(card, indent=2, ensure_ascii=False) + "\n")
            logger.info("card copy: %s %s written to %s/%s", card.get("id", "?"), card.get("version", ""), CARD_DIRNAME, CARD_METADATA)
        (card_dir / CARD_SUMMARY).write_text(json.dumps(summary, indent=2))
    except OSError as os_error:
        logger.error("card copy not written under %s: %s", card_dir, os_error)
        summary["error"] = f"{error}; " * bool(error) + str(os_error)
    return summary


def card_for_run(context: XnatContext | None, parent: dict | None, timeout: float = 60.0) -> tuple[dict | None, str]:
    """The card block for the run whose main container is ``parent`` (a CS container dict)."""
    if context is None:
        return None, "no XNAT context"
    if not parent:
        return None, "main container not found"
    return fetch_command_card(context, parent.get("command-id"), timeout)
