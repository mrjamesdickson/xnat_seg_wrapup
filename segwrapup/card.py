"""The card copy on every record (plan D27): the certificate of the run.

James, 2026-09-10: "In the output for a container we also need a copy of the card so people
can see where it came from and references etc. The certificate of the thing." The adopt tool
embeds the card bundle in the command definition as ``XNW_CARD_BUNDLE`` (base64 of a tar,
gzipped or not, holding ``metadata.json``, ``README.md``, ``command.json`` and ``LICENSE`` at
the adopted revision, a few KB), so the wrapup needs no network and the copy is exactly the
revision the run used. ``XNW_CARD_URL`` points at the registry's human-readable original.

Every wrapup (seg-wrapup, proc-wrapup, record-fetch's failure record) calls
:func:`write_card_copy`, which lays the bundle out under ``card/`` in the output and writes
``card/card.json`` summarising what the run actually used (card id and revision, image and
digest, wrapup and version, the bundle's files and checksum). ``card/**/*`` is part of the
``PROVENANCE`` default globs, so the copy rides onto the record with ``wrapup.json``. A missing
bundle is not an error (older cards have none; ``card.json`` still says what the environment
knew); a malformed one is logged and recorded in ``card.json`` as ``bundle_error``, never fatal:
the record and the dataset matter more than the certificate.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import io
import json
import logging
import os
import tarfile
from pathlib import Path, PurePosixPath

from . import __version__

logger = logging.getLogger("segwrapup.card")

CARD_BUNDLE_ENV = "XNW_CARD_BUNDLE"
CARD_URL_ENV = "XNW_CARD_URL"
CARD_DIRNAME = "card"
CARD_SUMMARY = "card.json"
#: The glob the PROVENANCE defaults carry for the copy (``Path.glob``: every file under card/).
CARD_GLOB = f"{CARD_DIRNAME}/**/*"
#: A bundle is a few KB; anything past this is not a card.
MAX_BUNDLE_BYTES = 4 * 1024 * 1024


class BundleError(ValueError):
    """The bundle cannot be laid out: not base64, not a tar, an unsafe member, too large."""


def _safe_member(member: tarfile.TarInfo) -> bool:
    """Only regular files at a relative path with no ``..`` and no absolute component."""
    if not member.isfile():
        return False
    parts = PurePosixPath(member.name).parts
    return bool(parts) and not member.name.startswith("/") and ".." not in parts and all(p not in ("", ".") for p in parts)


def unpack_bundle(encoded: str, dest: Path) -> tuple[list[str], str]:
    """Lay the base64 tar ``encoded`` out under ``dest``; return the file names written (sorted,
    relative, POSIX) and the bundle's sha256. Raises :class:`BundleError` with the reason."""
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise BundleError(f"{CARD_BUNDLE_ENV} is not base64: {error}") from error
    if len(raw) > MAX_BUNDLE_BYTES:
        raise BundleError(f"{CARD_BUNDLE_ENV} is {len(raw):,} bytes; a card bundle is at most {MAX_BUNDLE_BYTES:,}")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
    except tarfile.TarError as error:
        raise BundleError(f"{CARD_BUNDLE_ENV} is not a tar archive: {error}") from error
    written: list[str] = []
    total = 0
    with archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not _safe_member(member):
                raise BundleError(f"{CARD_BUNDLE_ENV} member {member.name!r} is not a regular file at a safe relative path")
            total += member.size
            if total > MAX_BUNDLE_BYTES:
                raise BundleError(f"{CARD_BUNDLE_ENV} unpacks past {MAX_BUNDLE_BYTES:,} bytes")
            source = archive.extractfile(member)
            if source is None:
                continue
            target = dest / PurePosixPath(member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
            written.append(PurePosixPath(member.name).as_posix())
    if not written:
        raise BundleError(f"{CARD_BUNDLE_ENV} holds no files")
    return sorted(written), digest


def write_card_copy(output_dir: Path, wrapup: str, environ: dict | None = None, extra: dict | None = None) -> dict:
    """Write ``card/`` (the bundle's files, when there is one) and ``card/card.json`` under
    ``output_dir``; return the summary that went into ``card.json``. Never raises."""
    env = os.environ if environ is None else environ
    card_dir = output_dir / CARD_DIRNAME
    summary = {
        "card_id": env.get("XNW_CARD_ID", ""), "card_revision": env.get("XNW_CARD_REVISION", ""),
        "card_url": env.get(CARD_URL_ENV, ""),
        "container_image": env.get("XNW_CONTAINER_IMAGE", ""), "container_digest": env.get("XNW_CONTAINER_DIGEST", ""),
        "wrapup": wrapup, "wrapup_version": __version__,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "bundle_files": [], "bundle_sha256": "",
        **(extra or {}),
    }
    encoded = (env.get(CARD_BUNDLE_ENV) or "").strip()
    try:
        card_dir.mkdir(parents=True, exist_ok=True)
        if encoded:
            summary["bundle_files"], summary["bundle_sha256"] = unpack_bundle(encoded, card_dir)
            logger.info("card copy: %d file(s) of %s %s laid out under %s/", len(summary["bundle_files"]),
                        summary["card_id"] or "the card", summary["card_revision"], CARD_DIRNAME)
        else:
            logger.info("no %s in the environment: the record carries card.json only (card %s %s)",
                        CARD_BUNDLE_ENV, summary["card_id"] or "?", summary["card_revision"])
        (card_dir / CARD_SUMMARY).write_text(json.dumps(summary, indent=2))
    except BundleError as error:
        logger.error("card copy not laid out for %s %s: %s", summary["card_id"] or "the card", summary["card_revision"], error)
        summary["bundle_error"] = str(error)
        try:
            (card_dir / CARD_SUMMARY).write_text(json.dumps(summary, indent=2))
        except OSError as write_error:
            logger.error("card.json not written under %s: %s", card_dir, write_error)
    except OSError as error:
        logger.error("card copy not written under %s: %s", card_dir, error)
        summary["bundle_error"] = str(error)
    return summary
