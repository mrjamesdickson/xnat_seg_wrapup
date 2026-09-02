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
