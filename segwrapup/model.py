"""
Model registration for training cards (proc-wrapup 0.7.1).

A dataset-scoped card whose results block says ``produces: model`` (``XNW_PRODUCES=model``)
trains something; its weights sit in ``DERIVED`` like any other output, named by the card's
``MODEL`` view. After the run record is published, this module registers the weights as an
``analysis:trainedModelData`` project asset in ``DRAFT``, with ``source_training_id`` pointing
at the run and ``source_dataset_id`` at the frozen dataset, uploads the weights to its ``MODEL``
resource (the model card, when the tool wrote one, to ``MODEL_CARD``; a small provenance file to
``PROVENANCE``), and writes ``produced_model_id`` back on the run record. Promotion out of
DRAFT is a person's act elsewhere (the grouplevel plugin's models page); nothing here decides
whether a model is any good.

Design: xnat_monailabel_plugin/docs/MONAI_TRAINING_FROM_DATASET_DESIGN.md §3.1; the link pair is
the prerequisite named in container-workshop/docs/DATASET-SCOPE-CARDS-DESIGN.md §7.2/§8.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from .publish import ANALYSIS_NS, DATASET_XSI_TYPE, XNAT_NS, RecordFile, _element, _put, publish_record
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


def model_label(dataset_label: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    tail = f"_{stamp}"
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


def build_model_xml(context: XnatContext, label: str, run_id: str, card: dict, contract: dict,
                    weights: list[str], dataset_facts: dict, when: datetime | None = None) -> str:
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
        _element("source_training_id", run_id),
        _element("source_dataset_id", context.dataset),
        _element("created_by", context.user),
        _element("created_time", now.strftime("%Y-%m-%dT%H:%M:%S")),
        _element("engine", framework),
        _element("container_image", card.get("container_image") or contract.get("container_image")),
        _element("engine_metadata_json", json.dumps({k: card.get(k) for k in ("app", "max_epochs", "train_cases", "val_cases",
                                                                              "val_split", "base_model", "train_stats", "card_id",
                                                                              "card_revision", "container_digest") if k in card})),
        _element("label_names", ",".join(f"{name}:{index}" for name, index in labels.items()) if labels else None),
        _element("num_classes", len(labels) + 1 if labels else None),
        _element("best_validation_dice", _best_dice(card)),
        _element("model_resource_label", MODEL_ROLE),
        _element("model_card_resource_label", MODEL_CARD_ROLE if card else None),
        _element("provenance_resource_label", PROVENANCE_ROLE),
        _element("default_checkpoint", card.get("default_checkpoint") or (weights[0] if len(weights) == 1 else None)),
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


def link_run_to_model(context: XnatContext, run_id: str, model_id: str, timeout_seconds: float = 60.0) -> None:
    """``produced_model_id`` on the run record: XNAT updates one field through the xpath query
    parameter on a bodiless PUT to the experiment."""
    field = urllib.parse.quote("analysis:GroupAnalysis/produced_model_id", safe="")
    url = (f"{context.host}/data/experiments/{urllib.parse.quote(run_id, safe='')}"
           f"?xsiType={urllib.parse.quote(DATASET_XSI_TYPE, safe='')}&{field}={urllib.parse.quote(model_id, safe='')}")
    _put(context, url, b"", "text/plain", timeout_seconds)


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
    label = model_label(dataset_facts.get("label") or context.dataset)
    weights = [Path(n).name for n in names]
    files: dict[str, list[RecordFile]] = {MODEL_ROLE: [RecordFile(root / n, Path(n).name) for n in names]}
    if card:
        card_path = next((root / n for n in (views or {}).get(MODEL_ROLE, []) if n.endswith(MODEL_CARD_FILENAME)), root / MODEL_CARD_FILENAME)
        files[MODEL_CARD_ROLE] = [RecordFile(card_path, MODEL_CARD_FILENAME)]
    provenance = {"registered_by": "proc-wrapup", "source_training_id": run_id, "source_training_label": run_outcome.get("label"),
                  "source_dataset_id": context.dataset, "dataset_label": dataset_facts.get("label"),
                  "card_id": contract.get("card_id"), "card_revision": contract.get("card_revision"),
                  "container_image": contract.get("container_image"), "container_digest": contract.get("container_digest"),
                  "weights": weights, "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    scratch = Path(tempfile.mkdtemp(prefix="proc-wrapup-model-"))
    (scratch / "provenance.json").write_text(json.dumps(provenance, indent=2))
    files[PROVENANCE_ROLE] = [RecordFile(scratch / "provenance.json", "provenance.json")]
    xml = build_model_xml(context, label, run_id, card, contract, weights, dataset_facts)
    try:
        outcome = publish_record(context, label, xml, files, timeout_seconds=timeout_seconds, xsi_type=MODEL_XSI_TYPE)
    except RuntimeError as error:
        logger.error("trained model %s not registered; the run record %s stays: %s", label, run_id, error)
        return {"label": label, "error": str(error)}
    result = {"xsi_type": MODEL_XSI_TYPE, "id": outcome["id"], "label": outcome["label"], "status": "DRAFT",
              "weights": weights, "model_card": bool(card), "source_training_id": run_id, "linked": False}
    try:
        link_run_to_model(context, run_id, outcome["id"], timeout_seconds=min(timeout_seconds, 60.0))
        result["linked"] = True
    except RuntimeError as error:
        # The model still points at the run through source_training_id; only the forward link is missing.
        logger.error("model %s registered but run %s could not be updated with produced_model_id: %s", outcome["id"], run_id, error)
        result["link_error"] = str(error)
    logger.info("trained model %s registered as %s (DRAFT) from run %s%s", label, outcome["id"], run_id,
                "" if result["linked"] else "; run not linked")
    return result
