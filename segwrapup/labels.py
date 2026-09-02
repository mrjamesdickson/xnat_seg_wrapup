"""Label tables: how a mask's integers get their names and colours.

Every model publishes its label map in a different file. This module reads the
common ones into a single ``dict[int, str]`` and renders the two viewer files
(ITK-SNAP ``labels.txt``, 3D Slicer ``labels.ctbl``) from it.
"""
from __future__ import annotations

import colorsys
import csv
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

LabelTable = dict[int, str]

# Filenames that are recognised automatically when found next to the masks.
AUTO_LABEL_FILENAMES = ("labels.json", "dataset.json", "metadata.json", "labels.txt", "labels.csv")


def _clean_names(pairs: dict) -> LabelTable:
    table: LabelTable = {}
    for key, value in pairs.items():
        try:
            table[int(key)] = str(value)
        except (TypeError, ValueError):
            logger.warning("Skipping label entry with non-integer index: %r -> %r", key, value)
    return table


def _from_mapping(mapping: dict) -> LabelTable:
    """Accept both ``{"1": "spleen"}`` and ``{"spleen": 1}`` orientations."""
    if not mapping:
        return {}
    first_key, first_value = next(iter(mapping.items()))
    if isinstance(first_value, int) or (isinstance(first_value, str) and first_value.isdigit()):
        return _clean_names({value: key for key, value in mapping.items() if not isinstance(value, list)})
    return _clean_names(mapping)


def load_nnunet_dataset_json(path: Path) -> LabelTable:
    """nnU-Net ``dataset.json``: v2 is name->int, v1 is int->name. Region lists are skipped."""
    data = json.loads(path.read_text())
    labels = data.get("labels")
    if not isinstance(labels, dict):
        raise ValueError(f"{path}: no 'labels' mapping")
    return _from_mapping(labels)


def load_monai_metadata_json(path: Path) -> LabelTable:
    """MONAI bundle ``metadata.json``: ``network_data_format.outputs.*.channel_def``."""
    data = json.loads(path.read_text())
    outputs = data.get("network_data_format", {}).get("outputs", {})
    for output_spec in outputs.values():
        channel_def = output_spec.get("channel_def")
        if isinstance(channel_def, dict):
            table = _clean_names(channel_def)
            if table:
                return table
    return {}


_ITKSNAP_LINE = re.compile(r'^\s*(\d+)\s+\d+\s+\d+\s+\d+\s+[\d.]+\s+\d\s+\d\s+"(.*)"\s*$')


def load_itksnap_label_file(path: Path) -> LabelTable:
    table: LabelTable = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ITKSNAP_LINE.match(line)
        if match:
            index = int(match.group(1))
            if index != 0:  # 0 is ITK-SNAP's reserved "Clear Label"
                table[index] = match.group(2)
        else:
            logger.warning("%s: unrecognised ITK-SNAP label line: %s", path, line.strip())
    return table


def load_label_csv(path: Path) -> LabelTable:
    """Two-column CSV (``label,name``), header optional."""
    table: LabelTable = {}
    with path.open(newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 2 or not row[0].strip().isdigit():
                continue
            table[int(row[0])] = row[1].strip()
    return table


def load_labels(path: Path) -> LabelTable:
    """Read any supported label file. Raises ValueError when the file cannot be parsed."""
    name = path.name.lower()
    try:
        if name == "dataset.json":
            return load_nnunet_dataset_json(path)
        if name == "metadata.json":
            return load_monai_metadata_json(path)
        if name.endswith(".json"):
            return _from_mapping(json.loads(path.read_text()))
        if name.endswith(".csv"):
            return load_label_csv(path)
        if name.endswith(".txt"):
            return load_itksnap_label_file(path)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read label file {path}: {error}") from error
    raise ValueError(f"unsupported label file: {path}")


def discover_labels(search_dir: Path) -> tuple[LabelTable, Path | None]:
    """Find a label file next to the masks (first match in AUTO_LABEL_FILENAMES order)."""
    for filename in AUTO_LABEL_FILENAMES:
        for candidate in sorted(search_dir.rglob(filename)):
            try:
                table = load_labels(candidate)
            except ValueError as error:
                logger.warning("Ignoring %s: %s", candidate, error)
                continue
            if table:
                return table, candidate
    return {}, None


def without_background(table: LabelTable) -> LabelTable:
    return {label: name for label, name in table.items() if label != 0 and name.lower() != "background"}


def label_color(label: int) -> tuple[int, int, int]:
    """Deterministic, well-separated RGB for a label index.

    Golden-angle hue rotation keeps neighbouring labels far apart in hue and gives
    the same structure the same colour on every run, which matters because the
    colours are written into the viewer files and mirrored in the HTML report.
    """
    hue = ((label - 1) * 137.508 % 360) / 360.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.62, 0.94)
    return int(round(red * 255)), int(round(green * 255)), int(round(blue * 255))


def itksnap_label_file(labels: LabelTable) -> str:
    """ITK-SNAP label description file: IDX R G B A VIS MSH LABEL. Index 0 is reserved."""
    lines = [
        "################################################",
        "# ITK-SnAP Label Description File",
        "# File format:",
        "# IDX   -R-  -G-  -B-  -A--  VIS MSH  LABEL",
        "# Fields:",
        "#    IDX:   Zero-based index",
        "#    -R-:   Red color component (0..255)",
        "#    -G-:   Green color component (0..255)",
        "#    -B-:   Blue color component (0..255)",
        "#    -A-:   Label transparency (0.00 .. 1.00)",
        "#    VIS:   Label visibility (0 or 1)",
        "#    MSH:   Label mesh visibility (0 or 1)",
        "#  LABEL:   Label description",
        "################################################",
        '    0     0    0    0        0  0  0    "Clear Label"',
    ]
    for label in sorted(labels):
        if label == 0:
            continue
        red, green, blue = label_color(label)
        name = labels[label].replace('"', "'")
        lines.append(f"{label:5d} {red:5d} {green:4d} {blue:4d}        1  1  1    \"{name}\"")
    return "\n".join(lines) + "\n"


def slicer_color_table(labels: LabelTable) -> str:
    """3D Slicer colour table (.ctbl): index name R G B A. Same colours as the ITK-SNAP file."""
    lines = ["# Color table file", "# 1 values", "0 Clear 0 0 0 0"]
    for label in sorted(labels):
        if label == 0:
            continue
        red, green, blue = label_color(label)
        name = labels[label].replace(" ", "_")
        lines.append(f"{label} {name} {red} {green} {blue} 255")
    return "\n".join(lines) + "\n"


def collect_labels(declared: LabelTable, results: list[dict]) -> LabelTable:
    """Every declared label plus any the masks actually contain.

    Declared-but-absent labels stay so the colour map is stable across subjects;
    observed-but-undeclared labels are added so nothing in the mask is unnamed.
    """
    labels = without_background(declared)
    for result in results:
        for structure in result["structures"]:
            labels.setdefault(structure["label"], structure["name"])
    return labels
