"""``seg-wrapup``: the Container Service wrapup entrypoint.

Reads the parent container's output at ``/input`` and writes the upload set to
``/output``. Policy on failure: a missing or unreadable mask fails the run (there
is nothing to upload); a report, label-file, or DICOM SEG failure is logged and the
masks are still delivered, because a segmentation without a report is useful and a
failed workflow with a good segmentation stranded in a build directory is not.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .labels import collect_labels, discover_labels, itksnap_label_file, load_labels, slicer_color_table
from .report import render_html
from .volumes import find_masks, looks_like_binary_set, measure_mask, merge_binary_masks

logger = logging.getLogger("seg-wrapup")

DEFAULT_SOURCE_DIRNAME = ".source_dicom"
MERGED_MASK_NAME = "segmentation.nii.gz"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="seg-wrapup", description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", type=Path, default=Path(os.environ.get("SEG_INPUT", "/input")))
    parser.add_argument("--output", type=Path, default=Path(os.environ.get("SEG_OUTPUT", "/output")))
    parser.add_argument("--model", default=os.environ.get("SEG_MODEL_NAME", "unknown"),
                        help="model name shown in the report and written into the DICOM SEG")
    parser.add_argument("--model-version", default=os.environ.get("SEG_MODEL_VERSION", "unknown"))
    parser.add_argument("--labels", type=Path, default=_env_path("SEG_LABELS"),
                        help="label table (labels.json, nnU-Net dataset.json, MONAI metadata.json, "
                             "ITK-SNAP labels.txt, or labels.csv); auto-discovered under --input when omitted")
    parser.add_argument("--source-dicom", type=Path, default=_env_path("SEG_SOURCE_DICOM"),
                        help=f"source DICOM series for the DICOM SEG; default <input>/{DEFAULT_SOURCE_DIRNAME}")
    parser.add_argument("--merge", choices=("auto", "yes", "no"), default=os.environ.get("SEG_MERGE", "auto"),
                        help="merge one-file-per-structure binary masks into one label map (auto: when detected)")
    parser.add_argument("--no-dicom-seg", action="store_true", help="skip DICOM SEG even if source DICOM is present")
    parser.add_argument("--session", default=os.environ.get("SEG_SESSION_LABEL", ""))
    parser.add_argument("--scan", default=os.environ.get("SEG_SCAN_ID", ""))
    return parser


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def run(args: argparse.Namespace) -> int:
    input_dir: Path = args.input
    output_dir: Path = args.output
    source_dicom: Path = args.source_dicom or (input_dir / DEFAULT_SOURCE_DIRNAME)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.is_dir():
        logger.error("input directory %s does not exist", input_dir)
        return 1

    masks = find_masks(input_dir, exclude=(source_dicom,))
    if not masks:
        logger.error("no NIfTI masks found under %s; nothing to deliver", input_dir)
        return 1

    label_table, label_source = {}, None
    if args.labels:
        try:
            label_table, label_source = load_labels(args.labels), args.labels
        except ValueError as error:
            logger.warning("label file rejected, falling back to numbers: %s", error)
    if not label_table:
        label_table, label_source = discover_labels(input_dir)
    logger.info("label table: %d entries from %s", len(label_table), label_source or "none")

    merge = args.merge == "yes" or (args.merge == "auto" and looks_like_binary_set(masks))
    delivered: list[Path] = []
    manifest: dict = {
        "wrapup_version": __version__,
        "model": args.model,
        "model_version": args.model_version,
        "label_source": str(label_source) if label_source else None,
        "merged": merge,
        "inputs": [str(path.relative_to(input_dir)) for path in masks],
    }

    if merge:
        merged_path = output_dir / MERGED_MASK_NAME
        try:
            merged_table = merge_binary_masks(masks, merged_path, label_table)
        except ValueError as error:
            logger.error("merge failed, delivering masks unmerged: %s", error)
            merge = False
        else:
            label_table = {**label_table, **merged_table}
            delivered.append(merged_path)
            manifest["merged_labels"] = merged_table
    if not merge:
        for mask in masks:
            destination = output_dir / mask.name
            if destination.exists():
                destination = output_dir / f"{mask.parent.name}_{mask.name}"
            shutil.copy2(mask, destination)
            delivered.append(destination)

    results = []
    for mask in delivered:
        try:
            results.append(measure_mask(mask, label_table))
        except Exception as error:  # noqa: BLE001 - one bad mask must not hide the others
            logger.error("could not measure %s: %s", mask.name, error)

    report = {
        "model": args.model,
        "model_version": args.model_version,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "session": args.session,
        "scan": args.scan,
        "results": results,
    }
    if results:
        try:
            write_report_files(output_dir, report, label_table, results)
        except OSError as error:
            logger.error("report files not written: %s", error)
    else:
        logger.error("no mask could be measured; report skipped")

    manifest["dicom_seg"] = None
    if args.no_dicom_seg:
        logger.info("DICOM SEG skipped by flag")
    elif not source_dicom.is_dir():
        logger.info("no source DICOM at %s; DICOM SEG skipped", source_dicom)
    elif len(delivered) != 1:
        logger.warning("DICOM SEG needs exactly one label map, found %d; skipped", len(delivered))
    else:
        from .dicomseg import write_dicom_seg

        try:
            manifest["dicom_seg"] = write_dicom_seg(
                delivered[0], source_dicom, label_table, output_dir / "segmentation.seg.dcm",
                model_name=args.model, model_version=args.model_version,
            )
        except Exception as error:  # noqa: BLE001 - SEG is additive; masks and report still ship
            logger.error("DICOM SEG not written for %s: %s", delivered[0].name, error)

    (output_dir / "wrapup.json").write_text(json.dumps(manifest, indent=2))
    for result in results:
        for item in result["structures"]:
            logger.info("  %s: %s mL (%s voxels)", item["name"], f"{item['volume_ml']:,.2f}", f"{item['voxels']:,}")
    logger.info("delivered %d mask(s) to %s", len(delivered), output_dir)
    return 0


def write_report_files(output_dir: Path, report: dict, label_table: dict, results: list[dict]) -> None:
    (output_dir / "volumes.json").write_text(json.dumps(report, indent=2))
    with (output_dir / "volumes.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "label", "structure", "voxels", "volume_ml"])
        for result in results:
            for item in result["structures"]:
                writer.writerow([result["file"], item["label"], item["name"], item["voxels"], item["volume_ml"]])
    (output_dir / "report.html").write_text(render_html(report))
    labels = collect_labels(label_table, results)
    if labels:
        (output_dir / "labels.txt").write_text(itksnap_label_file(labels))
        (output_dir / "labels.ctbl").write_text(slicer_color_table(labels))
    else:
        logger.warning("no labels to describe; label files skipped")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
