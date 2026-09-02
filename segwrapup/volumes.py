"""Find masks and measure them."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import nibabel as nib
import numpy as np

from .labels import LabelTable

logger = logging.getLogger(__name__)

NIFTI_SUFFIXES = (".nii", ".nii.gz")


def is_nifti(path: Path) -> bool:
    return path.is_file() and path.name.endswith(NIFTI_SUFFIXES)


def find_masks(root: Path, exclude: tuple[Path, ...] = ()) -> list[Path]:
    """Every NIfTI under ``root``, sorted, skipping anything under an excluded directory."""
    excluded = tuple(path.resolve() for path in exclude)
    masks = []
    for path in sorted(root.rglob("*")):
        if not is_nifti(path):
            continue
        resolved = path.resolve()
        if any(resolved == ex or ex in resolved.parents for ex in excluded):
            continue
        masks.append(path)
    return masks


def load_label_array(mask_path: Path) -> tuple[np.ndarray, nib.Nifti1Image]:
    image = nib.load(str(mask_path))
    data = np.asanyarray(image.dataobj)
    if data.ndim == 4 and data.shape[-1] == 1:
        data = data[..., 0]
    if data.ndim != 3:
        raise ValueError(f"{mask_path.name}: expected a 3-D label map, got shape {data.shape}")
    if not np.issubdtype(data.dtype, np.integer):
        rounded = np.rint(data)
        if not np.allclose(data, rounded):
            raise ValueError(f"{mask_path.name}: voxel values are not integer labels")
        data = rounded.astype(np.int32)
    return data, image


def measure_mask(mask_path: Path, label_names: LabelTable) -> dict:
    """Per-label volumes for one label map, in millilitres from the image's own spacing."""
    data, image = load_label_array(mask_path)
    zooms = image.header.get_zooms()[:3]
    voxel_mm3 = float(np.prod(zooms))
    voxel_ml = voxel_mm3 / 1000.0

    structures = []
    for label in np.unique(data):
        label = int(label)
        if label == 0:
            continue
        voxels = int((data == label).sum())
        name = label_names.get(label, f"label {label}")
        if name.lower() == "background":
            continue
        structures.append(
            {"label": label, "name": name, "voxels": voxels, "volume_ml": round(voxels * voxel_ml, 2)}
        )
    structures.sort(key=lambda item: item["volume_ml"], reverse=True)

    return {
        "file": mask_path.name,
        "shape": [int(dimension) for dimension in data.shape],
        "voxel_size_mm": [round(float(zoom), 4) for zoom in zooms],
        "voxel_volume_mm3": round(voxel_mm3, 4),
        "structures": structures,
        "total_volume_ml": round(sum(item["volume_ml"] for item in structures), 2),
    }


def structure_name_from_filename(path: Path) -> str:
    name = path.name
    for suffix in NIFTI_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def merge_binary_masks(mask_paths: list[Path], out_path: Path, label_names: LabelTable | None = None) -> LabelTable:
    """Combine one-file-per-structure binary masks into a single label map.

    This is TotalSegmentator's default output layout (``spleen.nii.gz``,
    ``liver.nii.gz``, ...). Labels are assigned by ``label_names`` when the file's
    stem matches a declared name, otherwise sequentially after the declared range,
    in sorted filename order so the assignment is reproducible. Where two masks
    overlap the later file wins; overlaps are logged.
    """
    if not mask_paths:
        raise ValueError("no masks to merge")
    declared = {name: label for label, name in (label_names or {}).items()}
    next_label = max(declared.values(), default=0) + 1

    merged: np.ndarray | None = None
    reference: nib.Nifti1Image | None = None
    table: LabelTable = {}
    for path in sorted(mask_paths):
        data, image = load_label_array(path)
        if merged is None:
            merged = np.zeros(data.shape, dtype=np.uint16)
            reference = image
        elif data.shape != merged.shape:
            raise ValueError(f"{path.name}: shape {data.shape} differs from first mask {merged.shape}")
        stem = structure_name_from_filename(path)
        label = declared.get(stem)
        if label is None:
            label = next_label
            next_label += 1
        foreground = data > 0
        overlap = int((foreground & (merged > 0)).sum())
        if overlap:
            logger.warning("%s overlaps %d voxels already labelled; later file wins", path.name, overlap)
        merged[foreground] = label
        table[label] = stem

    assert merged is not None and reference is not None
    nib.save(nib.Nifti1Image(merged, reference.affine, reference.header), str(out_path))
    return table


def merge_label_maps(mask_paths: list[Path], tables: list[LabelTable], out_path: Path) -> LabelTable:
    """Combine several multilabel maps into one by offsetting each map's labels.

    Used when one launch ran several models that each number their structures from 1
    (MOOSE). Maps are merged in the given order; map ``k``'s labels are shifted by the
    highest label used so far, so every structure keeps a unique index and its name.
    Later maps win where structures overlap; overlaps are logged.
    """
    if len(mask_paths) != len(tables) or not mask_paths:
        raise ValueError("merge_label_maps needs one label table per mask")
    merged: np.ndarray | None = None
    reference: nib.Nifti1Image | None = None
    combined: LabelTable = {}
    offset = 0
    for path, table in zip(mask_paths, tables):
        data, image = load_label_array(path)
        if merged is None:
            merged = np.zeros(data.shape, dtype=np.uint16)
            reference = image
        elif data.shape != merged.shape:
            raise ValueError(f"{path.name}: shape {data.shape} differs from first mask {merged.shape}")
        present = [int(v) for v in np.unique(data) if v != 0]
        if not present:
            logger.warning("%s contains no labels; skipped in merge", path.name)
            continue
        foreground = data > 0
        overlap = int((foreground & (merged > 0)).sum())
        if overlap:
            logger.warning("%s overlaps %d voxels already labelled; later map wins", path.name, overlap)
        merged[foreground] = (data[foreground].astype(np.int64) + offset).astype(np.uint16)
        for label in present:
            combined[label + offset] = table.get(label, f"{structure_name_from_filename(path)} label {label}")
        for label, name in table.items():
            if label != 0 and (label + offset) not in combined:
                combined[label + offset] = name
        offset += max(max(present), max(table, default=0))
    assert merged is not None and reference is not None
    nib.save(nib.Nifti1Image(merged, reference.affine, reference.header), str(out_path))
    return combined


_BINARY_LIKE = re.compile(r"^[01]$")


def looks_like_binary_set(mask_paths: list[Path]) -> bool:
    """True when there are several masks and every sampled one holds only {0, 1}."""
    if len(mask_paths) < 2:
        return False
    for path in mask_paths[: min(len(mask_paths), 5)]:
        data, _ = load_label_array(path)
        if not set(np.unique(data).tolist()) <= {0, 1}:
            return False
    return True


UINT8_MAX_LABEL = 255


def needs_uint8_companion(mask_path: Path) -> bool:
    """True when the map holds label values a byte cannot represent (e.g. MuscleMap's 1101..8162)."""
    data, _ = load_label_array(mask_path)
    return int(data.max()) > UINT8_MAX_LABEL


def write_uint8_companion(mask_path: Path, out_path: Path) -> dict[int, int]:
    """Write ``mask_path`` renumbered to consecutive 1..N as uint8.

    Viewers whose drawing layer is a byte (the XNAT workbench, ITK-SNAP's 8-bit
    mode) cannot load sparse or large label values, so ship a companion with the
    same renumbering the DICOM SEG uses (sorted present labels) and return the
    ``new -> original`` mapping so a label table can be written beside it.
    """
    data, image = load_label_array(mask_path)
    present = [int(v) for v in np.unique(data) if v != 0]
    if len(present) > UINT8_MAX_LABEL:
        raise ValueError(f"{mask_path.name}: {len(present)} labels present, more than a byte can hold")
    renumbered = np.zeros(data.shape, dtype=np.uint8)
    mapping: dict[int, int] = {}
    for new_value, original in enumerate(present, start=1):
        renumbered[data == original] = new_value
        mapping[new_value] = original
    out = nib.Nifti1Image(renumbered, image.affine, image.header)
    out.set_data_dtype(np.uint8)
    nib.save(out, str(out_path))
    return mapping
