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

import json
import logging
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

from .publish import ANALYSIS_NS, DATASET_XSI_TYPE, XNAT_NS, RecordFile, _element, _put, bounded_results_json, publish_record
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


def model_label(dataset_label: str, when: datetime | None = None, run_id: str | None = None) -> str:
    """``model_<dataset>_<stamp>_<run>``: the run's id (its part after ``XNAT_``) makes the label
    unique per run, so two training cards finishing on the same dataset in the same second do
    not collide on the create-only preflight (Codex P2, PR #21 round 4); the stamp keeps the
    labels sortable by hand."""
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    run = _LABEL_SAFE.sub("_", (run_id or "").split("_", 1)[-1]).strip("_")
    tail = f"_{stamp}" + (f"_{run}" if run else "")
    head = "model_" + _LABEL_SAFE.sub("_", dataset_label or "dataset").strip("_")
    return head[: LABEL_MAX - len(tail)] + tail


def read_model_card(root: Path, names: list[str]) -> dict:
    """The tool's ``model-card.json`` when it is among the MODEL view files or at the DERIVED
    root; ``{}`` otherwise. A broken card is logged and ignored: the weights still register."""
    candidates = [root / n for n in names if n.endswith(MODEL_CARD_FILENAME)] + [root / MODEL_CARD_FILENAME]
    for path in candidates:
        if path.is_file():
            try:
                data = json.loads(path.read_text())
                return data if isinstance(data, dict) else {}
            except (ValueError, OSError) as error:
                logger.warning("%s is not readable JSON (%s); the model registers without it", path.name, error)
                return {}
    return {}


def _best_dice(card: dict) -> float | None:
    stats = card.get("train_stats") if isinstance(card.get("train_stats"), dict) else {}
    for key in ("best_validation_dice", "best_metric"):
        value = stats.get(key, card.get(key))
        if isinstance(value, (int, float)):
            return float(value)
    return None


def default_checkpoint(declared, weights: list[str]) -> str | None:
    """The checkpoint a consumer loads first: the model card's value when it names one of the
    uploaded weights (exactly, or by basename), else the only weight, else nothing. A card value
    that is not among the weights (a stale `best.ckpt` beside an uploaded `best.pt`) would send
    every consumer to a file the MODEL resource does not hold (Codex P2, PR #21). A value that
    is not a string (a number, an object) is treated as undeclared, not raised: the card is the
    engine's, and registration is best effort after the run record is published."""
    if declared is not None and not isinstance(declared, str):
        logger.warning("model card default_checkpoint is a %s, not a string; treating it as undeclared", type(declared).__name__)
        declared = None
    declared = (declared or "").strip()
    if declared:
        if declared in weights:
            return declared
        by_name = [w for w in weights if w.rsplit("/", 1)[-1] == declared.rsplit("/", 1)[-1]]
        if len(by_name) == 1:
            logger.warning("model card default_checkpoint %r is not an uploaded path; using %r", declared, by_name[0])
            return by_name[0]
        logger.error("model card default_checkpoint %r is not among the uploaded weights %s; %s", declared, weights,
                     "falling back to the only weight" if len(weights) == 1 else "the model is registered without one")
    return weights[0] if len(weights) == 1 else None


def build_model_xml(context: XnatContext, label: str, run_id: str, card: dict, contract: dict,
                    weights: list[str], dataset_facts: dict, when: datetime | None = None, created_by: str | None = None) -> str:
    now = when or datetime.now(timezone.utc)
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
        _element("engine_metadata_json", json.dumps({**{k: card.get(k) for k in ("app", "max_epochs", "train_cases", "val_cases",
                                                                                 "val_split", "base_model", "train_stats", "card_id",
                                                                                 "card_revision", "container_digest") if k in card},
                                                     "source_run_id": run_id, "source_run_type": "analysis:groupAnalysisData"})),
        _element("label_names", ",".join(f"{name}:{index}" for name, index in labels.items()) if labels else None),
        _element("num_classes", len(labels) + 1 if labels else None),
        _element("best_validation_dice", _best_dice(card)),
        _element("model_resource_label", MODEL_ROLE),
        _element("model_card_resource_label", MODEL_CARD_ROLE if card else None),
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


def _record_fields(context: XnatContext, run_id: str, timeout: float) -> dict:
    url = f"{context.host}/data/experiments/{urllib.parse.quote(run_id, safe='')}?format=json"
    request = urllib.request.Request(url, headers=auth_headers(context))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode(errors="replace"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GET {url.split('?')[0]} failed: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as error:
        raise RuntimeError(f"GET {url.split('?')[0]} failed: {error}") from error
    try:
        return payload["items"][0]["data_fields"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"GET {url.split('?')[0]}: no data_fields in the answer") from error


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


def note_model_on_run(context: XnatContext, run_id: str, model_id: str, model_label: str, timeout_seconds: float = 60.0) -> None:
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
    try:
        current = json.loads(fields.get("results_json") or "{}")
        if not isinstance(current, dict):
            current = {"results": current}
    except ValueError:
        logger.warning("run %s results_json is not JSON; trained_model is written beside its raw text", run_id)
        current = {"results_raw": fields.get("results_json")}
    current["trained_model"] = {"id": model_id, "label": model_label, "xsi_type": MODEL_XSI_TYPE, "status": "DRAFT"}
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<analysis:GroupAnalysis xmlns:analysis="{ANALYSIS_NS}" xmlns:xnat="{XNAT_NS}" '
        f'ID="{escape(run_id)}" project="{escape(context.project)}" label="{escape(str(fields.get("label") or ""))}">\n'
        f"  <analysis:results_json>{escape(bounded_results_json(current))}</analysis:results_json>\n"
        "</analysis:GroupAnalysis>\n"
    )
    url = f"{context.host}/data/experiments/{urllib.parse.quote(run_id, safe='')}?xsiType={urllib.parse.quote(DATASET_XSI_TYPE, safe='')}"
    status, text = _put(context, url, xml.encode(), "application/xml", timeout_seconds)
    answered = text.strip()
    if answered and answered != run_id:
        # XNAT answers the id of the record it wrote; anything else means it made a new one
        raise RuntimeError(f"PUT {url.split('?')[0]} answered {answered!r}, not {run_id}: a record was created instead of updated")


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
    names = [n for n in (views or {}).get(MODEL_ROLE, []) if not n.endswith(MODEL_CARD_FILENAME)]
    if not names:
        logger.error("produces=model but the %s view names no weights on DERIVED; declare results.resources.%s in the card",
                     MODEL_ROLE, MODEL_ROLE)
        return {"error": f"no {MODEL_ROLE} view files on DERIVED"}
    root = output_dir / derived_root if derived_root else output_dir
    card = read_model_card(root, (views or {}).get(MODEL_ROLE, []))
    label = model_label(dataset_facts.get("label") or context.dataset, run_id=run_id)
    # view-relative names, not basenames: fold-0/best.pt and fold-1/best.pt are two files, and
    # default_checkpoint must name a path that exists on the MODEL resource (Codex P1, PR #21)
    weights = [n.replace(os.sep, "/") for n in names]
    files: dict[str, list[RecordFile]] = {MODEL_ROLE: [RecordFile(root / n, w) for n, w in zip(names, weights)]}
    if card:
        card_path = next((root / n for n in (views or {}).get(MODEL_ROLE, []) if n.endswith(MODEL_CARD_FILENAME)), root / MODEL_CARD_FILENAME)
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
    xml = build_model_xml(context, label, run_id, card, contract, weights, dataset_facts,
                          created_by=_username(context, min(timeout_seconds, 30.0)))
    try:
        outcome = publish_record(context, label, xml, files, timeout_seconds=timeout_seconds, xsi_type=MODEL_XSI_TYPE)
    except RuntimeError as error:
        logger.error("trained model %s not registered; the run record %s stays: %s", label, run_id, error)
        return {"label": label, "error": str(error)}
    result = {"xsi_type": MODEL_XSI_TYPE, "id": outcome["id"], "label": outcome["label"], "status": "DRAFT",
              "weights": weights, "model_card": bool(card), "source_run_id": run_id, "linked": False}
    try:
        note_model_on_run(context, run_id, outcome["id"], outcome["label"], timeout_seconds=min(timeout_seconds, 60.0))
        result["linked"] = True
    except RuntimeError as error:
        # The model still names the run in engine_metadata_json/provenance.json; only the forward link is missing.
        logger.error("model %s registered but run %s could not be noted in its results_json: %s", outcome["id"], run_id, error)
        result["link_error"] = str(error)
    logger.info("trained model %s registered as %s (DRAFT) from run %s%s", label, outcome["id"], run_id,
                "" if result["linked"] else "; run not linked")
    return result
