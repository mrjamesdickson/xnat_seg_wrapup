"""
Model registration for training cards (proc-wrapup 0.7.1).

A dataset-scoped card whose results block says ``produces: model`` (``XNW_PRODUCES=model``)
trains something; its weights sit in ``DERIVED`` like any other output, named by the card's
``MODEL`` view. After the run record is published, this module registers the weights as an
``analysis:trainedModelData`` project asset in ``DRAFT``, with ``source_dataset_id`` pointing at
the frozen dataset (the run itself is named in ``engine_metadata_json.source_run_id`` and in
``provenance.json``; see ``build_model_xml`` for why not ``source_training_id``), uploads the weights to its ``MODEL``
resource (the model card, when the tool wrote one, to ``MODEL_CARD``; a small provenance file to
``PROVENANCE``), and notes the model on the run record as ``results_json.trained_model``
(``produced_model_id`` exists only on ``analysis:groupTrainingData`` in schema plugin 0.2.0, not on
the group record; see ``note_model_on_run``). Promotion out of
DRAFT is a person's act elsewhere (the grouplevel plugin's models page); nothing here decides
whether a model is any good.

Design: xnat_monailabel_plugin/docs/MONAI_TRAINING_FROM_DATASET_DESIGN.md §3.1; the link pair is
the prerequisite named in container-workshop/docs/DATASET-SCOPE-CARDS-DESIGN.md §7.2/§8.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import http.client
import urllib.error
import urllib.request

from .publish import ANALYSIS_NS, DATASET_XSI_TYPE, RESULTS_JSON_MAX, XNAT_NS, RecordFile, _element, _put, bounded_results_json, publish_record
from .register import auth_headers
from .register import LABEL_MAX, XnatContext

logger = logging.getLogger("segwrapup.model")

MODEL_XSI_TYPE = "analysis:trainedModelData"
MODEL_ROLE = "MODEL"
MODEL_CARD_ROLE = "MODEL_CARD"
PROVENANCE_ROLE = "PROVENANCE"
MODEL_CARD_FILENAME = "model-card.json"
#: What a results block may say it produces. Anything else is a contract error.
PRODUCES_VALUES = ("", "model")
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


RUN_TOKEN_MAX = 20   # of the 64: the stamp takes 17, leaving at least 26 for the dataset head


#: ``<site id>_E<number>``, XNAT's experiment accession id. The site id is whatever the site configured
#: and may carry ``_``, ``-`` or ``.`` (``MY_SITE_E123``), the same token characters
#: ``publish.created_record_id`` accepts (Codex P2, PR #21 round 18).
_ACCESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*_E\d+$")


def model_label(dataset_label: str, when: datetime | None = None, run_id: str | None = None) -> str:
    """``model_<dataset>_<stamp>_<run>``: the run's id (its part after ``XNAT_``) makes the label
    unique per run, so two training cards finishing on the same dataset in the same second do
    not collide on the create-only preflight (Codex P2, PR #21 round 4); the stamp keeps the
    labels sortable by hand."""
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    # only an accession id (XNAT_E26051, CENTRAL_E7) loses its site prefix; publish_record falls back
    # to the run's *label* as its id when XNAT answers no id, and splitting that at its first
    # underscore would throw away the pipeline name, making trainerA_ds_stamp_record and
    # trainerB_ds_stamp_record the same model label (Codex P2, PR #21 round 11)
    token = run_id or ""
    if _ACCESSION_ID.match(token):
        token = "E" + token.rsplit("_E", 1)[1]   # the whole site prefix goes, whatever it contains
    run = _LABEL_SAFE.sub("_", token).strip("_")
    if len(run) > RUN_TOKEN_MAX:
        # publish_record falls back to the run's label as its id when XNAT answers no id; a label can
        # be 64 characters, and the tail must leave room for a head (Codex P2, PR #21 round 8)
        run = run[:RUN_TOKEN_MAX - 9] + "_" + hashlib.sha1(run.encode()).hexdigest()[:8]
    tail = f"_{stamp}" + (f"_{run}" if run else "")
    head = "model_" + _LABEL_SAFE.sub("_", dataset_label or "dataset").strip("_")
    return head[: LABEL_MAX - len(tail)] + tail


def is_model_card(name: str) -> bool:
    """Whether a view-relative file name is the tool's model card: the basename is exactly
    ``model-card.json`` (``fold-0/model-card.json`` is; ``backup-model-card.json`` is not)."""
    return Path(name).name == MODEL_CARD_FILENAME


def model_card_path(root: Path, names: list[str]) -> Path | None:
    """The tool's ``model-card.json`` when it is among the MODEL view files or at the DERIVED
    root, whatever it contains; None when there is no such file. Presence is tracked apart from
    content: an empty ``{}`` card is still the tool's card and goes on MODEL_CARD (Codex P2, PR #21
    round 11)."""
    # the basename, exactly: backup-model-card.json in a broad MODEL view is a weight-side file, not
    # the card (Codex P2, PR #21 round 15)
    candidates = [root / n for n in names if is_model_card(n)] + [root / MODEL_CARD_FILENAME]
    path = next((path for path in candidates if path.is_file()), None)
    if path is not None and path.stat().st_size == 0:
        # publish_record skips zero-byte files (XNAT refuses them), so advertising this one would send
        # consumers to a MODEL_CARD resource with nothing on it (Codex P2, PR #21 round 12)
        logger.warning("%s is empty; the model registers without a card", path.name)
        return None
    return path


def read_model_card(root: Path, names: list[str]) -> dict:
    """The metadata in the tool's ``model-card.json`` (:func:`model_card_path`); ``{}`` when there
    is no card or it holds no object. A broken card is logged and ignored: the weights still
    register, and the file still goes on MODEL_CARD."""
    path = model_card_path(root, names)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError, RecursionError) as error:
        # RecursionError: a syntactically valid card nested tens of thousands deep; parsed here,
        # before register_trained_model's guard, it would abort the wrapup after the run record
        # was published and before the manifest is written (Codex P2, round 24)
        logger.warning("%s is not readable JSON (%s); the model registers without its metadata", path.name, error or type(error).__name__)
        return {}


def _best_dice(card: dict) -> float | None:
    stats = card.get("train_stats") if isinstance(card.get("train_stats"), dict) else {}
    for key in ("best_validation_dice", "best_metric"):
        value = stats.get(key, card.get(key))
        if isinstance(value, bool):
            continue
        # a Dice is in [0, 1]; a 400-digit JSON integer would overflow float() while the model XML is
        # built, after the run record was published (Codex P2, PR #21 round 9): bound before converting.
        # A finite value outside [0, 1] (a loss, an epoch count under the generic best_metric) is not
        # a Dice and is not published as one (round 11).
        if isinstance(value, int) and -1_000_000 <= value <= 1_000_000:
            value = float(value)
        if isinstance(value, float) and math.isfinite(value):
            if 0.0 <= value <= 1.0:
                return value
            logger.warning("%s %r is outside [0, 1]; not a Dice, not recorded as best_validation_dice", key, value)
    return None


def num_classes(labels: dict) -> int | None:
    """The network's output classes from the declared label indices: the highest index plus one,
    which counts background whether the card lists it (``{"background": 0, "spleen": 1}`` is 2
    classes, not 3) or not (``{"spleen": 1}`` is 2 too) and survives sparse indices (Codex P2,
    PR #21 round 5). None when no index is a finite non-negative integer: ``1e309`` decodes as
    infinity and ``int()`` of it would raise after the run record was published (round 7)."""
    indices = [int(v) for v in (labels or {}).values() if _is_label_index(v)]
    return max(indices) + 1 if indices else None


MAX_LABEL_INDEX = 65535   # a segmentation stored as uint16 cannot hold more; a 400-digit "index" is not one


def _is_label_index(value) -> bool:
    """A finite, integral, non-negative index no larger than a mask can hold. Integers are checked
    as integers: a 400-digit JSON integer decodes fine and math.isfinite(int) would raise
    OverflowError converting it (Codex P2, PR #21 round 8)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= MAX_LABEL_INDEX
    if isinstance(value, float):
        return math.isfinite(value) and value.is_integer() and 0 <= value <= MAX_LABEL_INDEX
    return False


def default_checkpoint(declared, weights: list[str]) -> str | None:
    """The checkpoint a consumer loads first: the model card's value when it names one of the
    uploaded weights (exactly, or by basename when the card gave only a basename), else the only
    weight, else nothing. A card value that is not among the weights (a stale `best.ckpt` beside
    an uploaded `best.pt`) would send every consumer to a file the MODEL resource does not hold
    (Codex P2, PR #21). A path-qualified value that is missing (`fold-0/best.pt` when only
    `fold-1/best.pt` was uploaded) stays unresolved: matching it by basename, or handing over the
    only weight, would load a different fold from the one the card declared (Codex P2, round 22).
    A value that is not a string (a number, an object) is treated as undeclared, not raised: the
    card is the engine's, and registration is best effort after the run record is published."""
    if declared is not None and not isinstance(declared, str):
        logger.warning("model card default_checkpoint is a %s, not a string; treating it as undeclared", type(declared).__name__)
        declared = None
    declared = (declared or "").strip()
    if declared:
        if declared in weights:
            return declared
        if "/" in declared or "\\" in declared:
            # a Windows-style `fold-0\best.pt` from a portable card is a path too: read as a bare
            # basename it would fall through to the only-weight fallback and another fold (Codex P2, round 23)
            logger.error("model card default_checkpoint %r names a path that is not among the uploaded weights %s; "
                         "the model is registered without one rather than remapped to another file", declared, weights)
            return None
        by_name = [w for w in weights if w.rsplit("/", 1)[-1] == declared]
        if len(by_name) == 1:
            logger.warning("model card default_checkpoint %r is not an uploaded path; using %r", declared, by_name[0])
            return by_name[0]
        logger.error("model card default_checkpoint %r is not among the uploaded weights %s; %s", declared, weights,
                     "falling back to the only weight" if len(weights) == 1 else "the model is registered without one")
    return weights[0] if len(weights) == 1 else None


#: Every ``*_json`` element of the analysis schema is capped at 65,536 characters (see
#: :func:`segwrapup.publish.bounded_results_json`); ``engine_metadata_json`` is no exception.
ENGINE_METADATA_MAX = RESULTS_JSON_MAX

LINK_KEYS = ("source_run_id", "source_run_type")


def bounded_engine_metadata(meta: dict) -> str:
    """``engine_metadata_json`` under the schema cap. A detailed model card (per-epoch history in
    ``train_stats``) must not make XNAT refuse the record for weights that are otherwise fine
    (Codex P2, PR #21 round 10): first the ``train_stats`` lists and nested objects go (the
    scalars, best metric and epoch, stay), then ``train_stats`` as a whole, then everything but
    the run link. ``truncated`` names what was dropped; the full card is on ``MODEL_CARD``."""
    try:
        meta, replaced = _finite_json(meta)
    except RecursionError:
        # a card nested a thousand objects deep is no metadata a consumer can use; the run link is
        # kept and the rest named as dropped, the full card stays on MODEL_CARD (round 20)
        dropped = sorted(k for k in meta if k not in LINK_KEYS)
        logger.warning("engine_metadata_json is nested past the recursion limit; only the run link is kept, %s left off", dropped)
        return json.dumps({**{k: meta[k] for k in LINK_KEYS if k in meta}, "truncated": dropped}, allow_nan=False)
    if replaced:
        # a JSON number that overflowed to infinity, or a NaN the decoder accepted, would be written as
        # the non-standard token Infinity/NaN and strict consumers could not parse the metadata (round 19)
        meta = dict(meta, non_finite_values_replaced=replaced)
    text = json.dumps(meta, allow_nan=False)
    if len(text) <= ENGINE_METADATA_MAX:
        return text
    reduced = dict(meta)
    stats = reduced.get("train_stats")
    if isinstance(stats, dict):
        reduced["train_stats"] = {k: v for k, v in stats.items() if not isinstance(v, (list, dict))}
        reduced["truncated"] = ["train_stats"]
        candidate = json.dumps(reduced, allow_nan=False)
        if len(candidate) <= ENGINE_METADATA_MAX:
            logger.warning("engine_metadata_json would be %d characters (cap %d); train_stats is reduced to its scalars, "
                           "the full card is on MODEL_CARD", len(text), ENGINE_METADATA_MAX)
            return candidate
    reduced.pop("train_stats", None)
    reduced["truncated"] = ["train_stats"]
    candidate = json.dumps(reduced, allow_nan=False)
    if len(candidate) <= ENGINE_METADATA_MAX:
        logger.warning("engine_metadata_json would be %d characters (cap %d); train_stats is left off, "
                       "the full card is on MODEL_CARD", len(text), ENGINE_METADATA_MAX)
        return candidate
    dropped = sorted(k for k in meta if k not in LINK_KEYS)
    logger.warning("engine_metadata_json would be %d characters (cap %d); only the run link is kept, %s left off, "
                   "the full card is on MODEL_CARD", len(text), ENGINE_METADATA_MAX, dropped)
    return json.dumps({**{k: meta[k] for k in LINK_KEYS if k in meta}, "truncated": dropped}, allow_nan=False)


def _finite_json(value):
    """``value`` with every non-finite float replaced by None, and how many were: JSON has no
    Infinity or NaN, whatever Python's encoder writes by default."""
    if isinstance(value, float) and not math.isfinite(value):
        return None, 1
    if isinstance(value, dict):
        out, n = {}, 0
        for k, v in value.items():
            out[k], m = _finite_json(v)
            n += m
        return out, n
    if isinstance(value, (list, tuple)):
        out, n = [], 0
        for v in value:
            item, m = _finite_json(v)
            out.append(item)
            n += m
        return out, n
    return value, 0


def build_model_xml(context: XnatContext, label: str, run_id: str, card: dict, contract: dict,
                    weights: list[str], dataset_facts: dict, when: datetime | None = None, created_by: str | None = None,
                    model_card: bool | None = None) -> str:
    now = when or datetime.now(timezone.utc)
    has_card = bool(card) if model_card is None else model_card   # the file's presence, not its content
    labels = card.get("labels") if isinstance(card.get("labels"), dict) else {}
    framework = card.get("model_framework") or contract.get("analysis_type") or ""
    model_name = card.get("model") or contract.get("pipeline") or "model"
    body = "".join(x for x in [
        _element("description", f"Trained by {contract.get('pipeline') or 'a training card'} on dataset "
                                f"{dataset_facts.get('label') or context.dataset}; registered by proc-wrapup from run {run_id}"),
        _element("model_name", model_name),
        _element("model_version", card.get("card_revision") or contract.get("card_revision")),
        _element("model_family", framework),
        _element("model_status", "DRAFT"),
        _element("source_dataset_id", context.dataset),
        _element("created_by", created_by or context.user),
        _element("created_time", now.strftime("%Y-%m-%dT%H:%M:%S")),
        _element("engine", framework),
        _element("container_image", card.get("container_image") or contract.get("container_image")),
        # source_training_id is NOT written: in xnat-analysis-schema-plugin <= 0.2.0 it is a foreign key
        # to analysis:groupTrainingData (the type the JAR commands wrote, on the drop list), and the run
        # here is an analysis:groupAnalysisData, so XNAT answers 500 (FK violation, demo02 2026-09-26:
        # "analysis_trainedmodeldata_source_training_id_fkey"). The run id rides in engine_metadata_json
        # and provenance.json instead, and the run names the model in results_json.trained_model.
        # Re-pointing source_training_id at the group record is a schema change for the analysis
        # plugin, not for this wrapup.
        _element("engine_metadata_json", bounded_engine_metadata({**{k: card.get(k) for k in ("app", "max_epochs", "train_cases", "val_cases",
                                                                                             "val_split", "base_model", "train_stats", "card_id",
                                                                                             "card_revision", "container_digest") if k in card},
                                                                  "source_run_id": run_id, "source_run_type": "analysis:groupAnalysisData"})),
        _element("label_names", ",".join(f"{name}:{index}" for name, index in labels.items()) if labels else None),
        _element("num_classes", num_classes(labels)),
        _element("best_validation_dice", _best_dice(card)),
        _element("model_resource_label", MODEL_ROLE),
        _element("model_card_resource_label", MODEL_CARD_ROLE if has_card else None),
        _element("provenance_resource_label", PROVENANCE_ROLE),
        _element("default_checkpoint", default_checkpoint(card.get("default_checkpoint"), weights)),
        _element("task_type", card.get("task_type")),
        _element("model_framework", framework),
    ] if x)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<analysis:TrainedModel xmlns:analysis="{ANALYSIS_NS}" xmlns:xnat="{XNAT_NS}" '
        f'project="{escape(context.project)}" label="{escape(label)}">\n'
        f"  <xnat:date>{now.strftime('%Y-%m-%d')}</xnat:date>\n"
        f"{body}</analysis:TrainedModel>\n"
    )


def _record_fields(context: XnatContext, run_id: str, timeout: float, by_label: bool | None = None) -> dict:
    """The run record's data_fields, looked up by accession id, or by label under the project when
    ``run_id`` is the fallback label publish_record hands back when XNAT answered no id (Codex P2,
    PR #21 round 16). ``by_label`` says which when the caller knows (the create's answer says);
    only without it is the id's shape consulted, since a label may look like an accession
    (round 19). The answer's ``ID`` is the accession the link is keyed on."""
    if by_label is None:
        by_label = not _ACCESSION_ID.match(run_id or "")
    if not by_label:
        url = f"{context.host}/data/experiments/{urllib.parse.quote(run_id, safe='')}?format=json"
    else:
        url = (f"{context.host}/data/projects/{urllib.parse.quote(context.project, safe='')}/experiments/"
               f"{urllib.parse.quote(run_id, safe='')}?format=json")
    request = urllib.request.Request(url, headers=auth_headers(context))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode(errors="replace"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GET {url.split('?')[0]} failed: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as error:
        raise RuntimeError(f"GET {url.split('?')[0]} failed: {error}") from error
    try:
        fields = payload["items"][0]["data_fields"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"GET {url.split('?')[0]}: no data_fields in the answer") from error
    if not isinstance(fields, dict):
        # valid JSON of the wrong shape must be the RuntimeError register_trained_model handles, not
        # an AttributeError out of note_model_on_run after run and model exist (Codex P2, round 11)
        raise RuntimeError(f"GET {url.split('?')[0]}: data_fields is {type(fields).__name__}, not an object")
    return fields


def _username(context: XnatContext, timeout: float) -> str:
    """The person the run belongs to. ``context.user`` is the alias token the Container Service
    handed the container (live: ``created_by`` read ``e720932c-...`` on demo02, 2026-09-26);
    ``GET /xapi/users/username`` answers the real login for that token."""
    url = f"{context.host}/xapi/users/username"
    request = urllib.request.Request(url, headers=auth_headers(context))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            name = response.read().decode(errors="replace").strip().strip('"')
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as error:
        logger.warning("could not resolve the launching user (%s); created_by keeps the alias", error)
        return context.user
    return name or context.user


def _current_results(run_id: str, raw) -> dict:
    """The run's ``results_json`` as a dict to merge ``trained_model`` into. XNAT answers the
    element as a string, but a JSON-native value (a dict, a number) must not raise TypeError out
    of the link step after run and model exist (Codex P2, PR #21 round 14): a dict is taken as is,
    anything else that is not JSON text is kept under ``results``."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        logger.warning("run %s results_json is a %s, not text; trained_model is written beside it", run_id, type(raw).__name__)
        return {"results": raw}
    try:
        current = json.loads(raw)
    except ValueError:
        logger.warning("run %s results_json is not JSON; trained_model is written beside its raw text", run_id)
        return {"results_raw": raw}
    return current if isinstance(current, dict) else {"results": current}


def resolve_accession(context: XnatContext, record_id: str, timeout_seconds: float = 60.0, is_accession: bool | None = None) -> str:
    """The accession id behind ``record_id``: itself when it is one, else the ``ID`` of the record
    looked up by that label under the project. publish_record hands back the label when XNAT
    answered a create with no id, and says so (``id_is_accession``); a label is project-scoped
    and no reverse link, so everything that names the run or the model by id resolves it first
    (Codex P2, PR #21 round 17). ``is_accession`` is that answer; a label may itself look like an
    accession (``MY_SITE_E123``), so the shape decides only when nobody knows (round 19). Raises
    RuntimeError when it cannot be resolved."""
    record_id = (record_id or "").strip()
    if not record_id:
        raise RuntimeError("no record id to resolve")
    if is_accession is None:
        is_accession = bool(_ACCESSION_ID.match(record_id))
    if is_accession:
        return record_id
    accession = str(_record_fields(context, record_id, timeout_seconds, by_label=True).get("ID") or "").strip()
    if not _ACCESSION_ID.match(accession):
        raise RuntimeError(f"record {record_id}: the accession id could not be resolved from its data_fields ({accession!r})")
    logger.info("record label %s resolved to %s", record_id, accession)
    return accession


def note_model_on_run(context: XnatContext, run_id: str, model_id: str, model_label: str, timeout_seconds: float = 60.0) -> str:
    """Write ``results_json.trained_model`` on the run record by a partial XML PUT keyed on the
    record's ID.

    Why not ``produced_model_id``: in xnat-analysis-schema-plugin 0.2.0 that element belongs to
    ``analysis:groupTrainingData`` (an extension of the group record, on the drop list), not to
    ``analysis:groupAnalysisData``, so the group record has no such field. And why not the
    query-parameter form (``PUT /data/experiments/<id>?xsiType=...&analysis:groupAnalysisData/x=y``):
    live on demo02 (2026-09-26) XNAT answered 422 "must include the project attribute" on the
    experiment path and, on the project path or with a project field, CREATED a second record
    labelled with the run's id instead of updating it. A partial XML document carrying the ID,
    project and label merges into the existing record (XNAT_E26034 updated in place, no stray).
    ``results_json`` is re-read first so nothing the publish wrote is lost."""
    fields = _record_fields(context, run_id, timeout_seconds)
    accession = str(fields.get("ID") or "").strip()
    if not _ACCESSION_ID.match(accession):
        # the partial document is keyed on the accession; a label (the fallback id) would create a stray
        raise RuntimeError(f"run {run_id}: the record's accession id could not be resolved from its data_fields ({accession!r})")
    current = _current_results(accession, fields.get("results_json"))
    current["trained_model"] = {"id": model_id, "label": model_label, "xsi_type": MODEL_XSI_TYPE, "status": "DRAFT"}
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<analysis:GroupAnalysis xmlns:analysis="{ANALYSIS_NS}" xmlns:xnat="{XNAT_NS}" '
        f'ID="{escape(accession)}" project="{escape(context.project)}" label="{escape(str(fields.get("label") or ""))}">\n'
        f"  <analysis:results_json>{escape(bounded_results_json(current))}</analysis:results_json>\n"
        "</analysis:GroupAnalysis>\n"
    )
    url = f"{context.host}/data/experiments/{urllib.parse.quote(accession, safe='')}?xsiType={urllib.parse.quote(DATASET_XSI_TYPE, safe='')}"
    status, text = _put(context, url, xml.encode(), "application/xml", timeout_seconds)
    answered = text.strip()
    if answered and answered != accession:
        # XNAT answers the id of the record it wrote; anything else means it made a new one
        raise RuntimeError(f"PUT {url.split('?')[0]} answered {answered!r}, not {accession}: a record was created instead of updated")
    return accession


def register_trained_model(context: XnatContext, output_dir: Path, derived_root: str | None, run_outcome: dict,
                           run_status: str, views: dict[str, list[str]], contract: dict, dataset_facts: dict,
                           timeout_seconds: float = 300.0) -> dict:
    """Register the run's weights as a DRAFT ``trainedModelData`` and link the run to it. Never
    raises: the run record is already published and stays; every failure is returned in the
    outcome (and logged) so ``wrapup.json`` says what did not happen."""
    run_id = run_outcome.get("id") or ""
    if context.scope != "dataset":
        return {"error": f"produces=model is only defined at dataset scope (this run is {context.scope}-scoped)"}
    if run_status != "SUCCEEDED":
        logger.warning("run %s is %s; no model is registered from a run that did not succeed", run_id, run_status)
        return {"skipped": f"run {run_status}"}
    try:
        # the model's reverse link (engine_metadata_json.source_run_id, provenance.json) must be an
        # accession a consumer can GET, not the label publish_record fell back to (round 17)
        run_id = resolve_accession(context, run_id, min(timeout_seconds, 60.0), is_accession=run_outcome.get("id_is_accession"))
    except RuntimeError as error:
        logger.error("no model registered: the run's accession id is unknown (%s)", error)
        return {"error": f"the run's accession id could not be resolved: {error}"}
    names = [n for n in (views or {}).get(MODEL_ROLE, []) if not is_model_card(n)]
    if not names:
        logger.error("produces=model but the %s view names no weights on DERIVED; declare results.resources.%s in the card",
                     MODEL_ROLE, MODEL_ROLE)
        return {"error": f"no {MODEL_ROLE} view files on DERIVED"}
    root = output_dir / derived_root if derived_root else output_dir
    card = read_model_card(root, (views or {}).get(MODEL_ROLE, []))
    card_path = model_card_path(root, (views or {}).get(MODEL_ROLE, []))
    label = model_label(dataset_facts.get("label") or context.dataset, run_id=run_id)
    # view-relative names, not basenames: fold-0/best.pt and fold-1/best.pt are two files, and
    # default_checkpoint must name a path that exists on the MODEL resource (Codex P1, PR #21)
    weights = [n.replace(os.sep, "/") for n in names]
    files: dict[str, list[RecordFile]] = {MODEL_ROLE: [RecordFile(root / n, w) for n, w in zip(names, weights)]}
    if card_path is not None:
        # the tool's card goes up whatever it says, an empty {} included (Codex P2, PR #21 round 11)
        files[MODEL_CARD_ROLE] = [RecordFile(card_path, MODEL_CARD_FILENAME)]
    provenance = {"registered_by": "proc-wrapup", "source_run_id": run_id, "source_run_type": "analysis:groupAnalysisData",
                  "source_run_label": run_outcome.get("label"),
                  "source_dataset_id": context.dataset, "dataset_label": dataset_facts.get("label"),
                  "card_id": contract.get("card_id"), "card_revision": contract.get("card_revision"),
                  "container_image": contract.get("container_image"), "container_digest": contract.get("container_digest"),
                  "weights": weights, "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    scratch = Path(tempfile.mkdtemp(prefix="proc-wrapup-model-"))
    (scratch / "provenance.json").write_text(json.dumps(provenance, indent=2))
    files[PROVENANCE_ROLE] = [RecordFile(scratch / "provenance.json", "provenance.json")]
    try:
        # built inside the guard: a card the document cannot be made from (a lone surrogate that fails
        # at xml.encode(), metadata nested past the recursion limit) is an outcome, not an abort
        # after the run record exists (Codex P2, rounds 15 and 20)
        xml = build_model_xml(context, label, run_id, card, contract, weights, dataset_facts,
                              created_by=_username(context, min(timeout_seconds, 30.0)), model_card=card_path is not None)
        outcome = publish_record(context, label, xml, files, timeout_seconds=timeout_seconds, xsi_type=MODEL_XSI_TYPE)
    except (RuntimeError, UnicodeEncodeError, RecursionError, ValueError, TypeError) as error:
        logger.error("trained model %s not registered; the run record %s stays: %s", label, run_id, error)
        return {"label": label, "error": str(error) if isinstance(error, RuntimeError) else f"{type(error).__name__}: {error}"}
    result = {"xsi_type": MODEL_XSI_TYPE, "id": outcome["id"], "label": outcome["label"], "status": "DRAFT",
              "weights": weights, "model_card": card_path is not None, "source_run_id": run_id, "linked": False}
    try:
        # the forward link names the model by accession too; a create that answered no id is looked up by label
        result["id"] = resolve_accession(context, outcome["id"], min(timeout_seconds, 60.0), is_accession=outcome.get("id_is_accession"))
        result["source_run_id"] = note_model_on_run(context, run_id, result["id"], outcome["label"], timeout_seconds=min(timeout_seconds, 60.0))
        result["linked"] = True
    except (RuntimeError, UnicodeEncodeError) as error:
        # The model still names the run in engine_metadata_json/provenance.json; only the forward link is missing.
        logger.error("model %s registered but run %s could not be noted in its results_json: %s", outcome["id"], run_id, error)
        result["link_error"] = str(error)
    logger.info("trained model %s registered as %s (DRAFT) from run %s%s", label, outcome["id"], run_id,
                "" if result["linked"] else "; run not linked")
    return result
