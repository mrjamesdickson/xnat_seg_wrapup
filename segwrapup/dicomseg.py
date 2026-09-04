"""Build a DICOM Segmentation object from a NIfTI label map and its source series.

The NIfTI is aligned to the source series' voxel grid by axis permutation and flip
only (what a dcm2niix-derived mask needs). A mask on a different grid is refused
rather than resampled, because a silently resampled overlay is worse than none.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path

import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import Dataset

from .labels import LabelTable

logger = logging.getLogger(__name__)

SEG_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.66.4"
_NON_IMAGE_SOP_CLASSES = {SEG_SOP_CLASS_UID, "1.2.840.10008.5.1.4.1.1.481.3"}  # SEG, RTSTRUCT


def load_series(dicom_dir: Path) -> list[Dataset]:
    """Read one image series from a directory, sorted along the slice normal.

    If several series are present the one with the most instances is used and a
    warning is logged; a wrapup has no way to ask which one was meant.
    """
    by_series: dict[str, list[Dataset]] = {}
    for path in sorted(p for p in dicom_dir.rglob("*") if p.is_file()):
        try:
            dataset = pydicom.dcmread(str(path), force=False)
        except (pydicom.errors.InvalidDicomError, OSError, ValueError) as error:
            logger.debug("Skipping non-DICOM file %s: %s", path, error)
            continue
        sop_class = str(getattr(dataset, "SOPClassUID", ""))
        if sop_class in _NON_IMAGE_SOP_CLASSES or "ImagePositionPatient" not in dataset:
            continue
        by_series.setdefault(str(dataset.SeriesInstanceUID), []).append(dataset)

    if not by_series:
        raise ValueError(f"no DICOM image instances found under {dicom_dir}")
    if len(by_series) > 1:
        logger.warning("%d series under %s; using the largest", len(by_series), dicom_dir)
    datasets = max(by_series.values(), key=len)

    orientation = np.array(datasets[0].ImageOrientationPatient, dtype=float)
    normal = np.cross(orientation[:3], orientation[3:])
    datasets.sort(key=lambda ds: float(np.dot(np.array(ds.ImagePositionPatient, dtype=float), normal)))
    return datasets


def series_affine_ras(datasets: list[Dataset]) -> np.ndarray:
    """Voxel (column, row, slice) -> RAS mm affine for a sorted series."""
    first = datasets[0]
    orientation = np.array(first.ImageOrientationPatient, dtype=float)
    row_spacing, column_spacing = (float(v) for v in first.PixelSpacing)
    column_direction = orientation[:3] * column_spacing
    row_direction = orientation[3:] * row_spacing
    origin = np.array(first.ImagePositionPatient, dtype=float)
    if len(datasets) > 1:
        last = np.array(datasets[-1].ImagePositionPatient, dtype=float)
        slice_direction = (last - origin) / (len(datasets) - 1)
    else:
        thickness = float(getattr(first, "SliceThickness", 1.0) or 1.0)
        slice_direction = np.cross(orientation[:3], orientation[3:]) * thickness

    lps = np.eye(4)
    lps[:3, 0] = column_direction
    lps[:3, 1] = row_direction
    lps[:3, 2] = slice_direction
    lps[:3, 3] = origin
    return np.diag([-1.0, -1.0, 1.0, 1.0]) @ lps


def align_mask_to_series(mask_image: nib.Nifti1Image, datasets: list[Dataset]) -> np.ndarray:
    """Return the mask as (frames, rows, columns) in the series' own voxel order."""
    data = np.asanyarray(mask_image.dataobj)
    if data.ndim == 4 and data.shape[-1] == 1:
        data = data[..., 0]
    target_affine = series_affine_ras(datasets)

    source_orientation = nib.orientations.io_orientation(mask_image.affine)
    target_orientation = nib.orientations.io_orientation(target_affine)
    transform = nib.orientations.ornt_transform(source_orientation, target_orientation)
    aligned = nib.orientations.apply_orientation(data, transform)

    expected = (int(datasets[0].Columns), int(datasets[0].Rows), len(datasets))
    if tuple(aligned.shape) != expected:
        raise ValueError(
            f"mask grid {tuple(aligned.shape)} (after reorientation) does not match the "
            f"series grid {expected}; refusing to resample"
        )

    aligned_affine = mask_image.affine @ nib.orientations.inv_ornt_aff(transform, data.shape)
    if not np.allclose(aligned_affine[:3, :3], target_affine[:3, :3], atol=0.05):
        logger.warning("mask spacing/orientation differs from the series beyond 0.05 mm; overlay may be shifted")
    if not np.allclose(aligned_affine[:3, 3], target_affine[:3, 3], atol=1.0):
        logger.warning("mask origin differs from the series by more than 1 mm; overlay may be shifted")

    return np.ascontiguousarray(np.transpose(aligned, (2, 1, 0)))


# Type 2 patient/study attributes highdicom copies from the source images by direct
# attribute access. DICOM requires them to be present (possibly empty); anonymisers
# such as TCIA's strip them outright, which made the SEG builder raise
# "'FileDataset' object has no attribute 'PatientBirthDate'" on a de-identified series.
_TYPE2_PATIENT_STUDY_ATTRIBUTES = (
    "PatientName", "PatientID", "PatientBirthDate", "PatientSex",
    "StudyDate", "StudyTime", "StudyID", "AccessionNumber", "ReferringPhysicianName",
)


def ensure_type2_patient_study_attributes(datasets: list[Dataset]) -> list[str]:
    """Add any missing Type 2 patient/study attribute to every dataset as an empty value.

    Returns the names that were missing on the first dataset, for the log.
    """
    missing = [name for name in _TYPE2_PATIENT_STUDY_ATTRIBUTES if name not in datasets[0]]
    for dataset in datasets:
        for name in _TYPE2_PATIENT_STUDY_ATTRIBUTES:
            if name not in dataset:
                setattr(dataset, name, "")
    if missing:
        logger.info("source series lacks %s (de-identified?); SEG carries them empty", ", ".join(missing))
    return missing


def unshare_segment_identification(segmentation) -> bool:
    """Move SegmentIdentificationSequence out of the shared groups and into every frame.

    highdicom hoists a functional group macro into ``SharedFunctionalGroupsSequence`` when
    its value is the same for every frame. For a one-label mask that is always true of the
    segment identification macro, so a single-lesion SEG carries it shared. That is legal
    DICOM, but XNAT's ROI plugin reads the macro from the per-frame groups only and rejects
    the collection with HTTP 500 "SegmentIdentification missing" -- which made every
    single-lesion model fail to register while multi-structure models were unaffected.

    The macro must not appear in both places, so it is moved rather than copied. Returns
    whether anything was moved, and is a no-op on the multi-segment path where highdicom
    already writes it per frame.
    """
    shared = segmentation.SharedFunctionalGroupsSequence[0]
    if "SegmentIdentificationSequence" not in shared:
        return False
    identification = shared.SegmentIdentificationSequence
    del shared.SegmentIdentificationSequence
    for frame in segmentation.PerFrameFunctionalGroupsSequence:
        frame.SegmentIdentificationSequence = deepcopy(identification)
    logger.info("moved SegmentIdentificationSequence into %d per-frame groups for XNAT's ROI plugin",
                len(segmentation.PerFrameFunctionalGroupsSequence))
    return True


def write_dicom_seg(
    mask_path: Path,
    dicom_dir: Path,
    labels: LabelTable,
    out_path: Path,
    model_name: str,
    model_version: str = "unknown",
    series_description: str | None = None,
) -> dict:
    """Write a BINARY DICOM SEG for ``mask_path`` against the series in ``dicom_dir``.

    Segment numbers in DICOM must be consecutive from 1, so sparse label values are
    renumbered; the returned mapping records ``segment_number -> original label``.
    """
    import highdicom as hd
    from highdicom.color import CIELabColor
    from highdicom.sr.coding import Code
    from pydicom.sr.codedict import codes

    from .labels import label_color

    datasets = load_series(dicom_dir)
    ensure_type2_patient_study_attributes(datasets)
    frames = align_mask_to_series(nib.load(str(mask_path)), datasets)

    present = [int(v) for v in np.unique(frames) if v != 0]
    if not present:
        raise ValueError(f"{mask_path.name}: mask is empty, nothing to encode")

    renumbered = np.zeros(frames.shape, dtype=np.uint8 if len(present) < 256 else np.uint16)
    descriptions = []
    mapping: dict[int, int] = {}
    anatomical_structure = Code("123037004", "SCT", "Anatomical Structure")
    for segment_number, label in enumerate(present, start=1):
        renumbered[frames == label] = segment_number
        mapping[segment_number] = label
        descriptions.append(
            hd.seg.SegmentDescription(
                segment_number=segment_number,
                segment_label=labels.get(label, f"label {label}")[:64],
                segmented_property_category=anatomical_structure,
                segmented_property_type=anatomical_structure,
                algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,
                algorithm_identification=hd.AlgorithmIdentificationSequence(
                    name=model_name[:64], family=codes.DCM.ArtificialIntelligence, version=model_version[:64]
                ),
                # RecommendedDisplayCIELabValue: the same colour as the report chips and
                # the ITK-SNAP / Slicer / BIDS label files, so OHIF and other SEG
                # consumers show the structure the way the rest of the resource does.
                display_color=CIELabColor.from_rgb(*label_color(label)),
            )
        )

    segmentation = hd.seg.Segmentation(
        source_images=datasets,
        pixel_array=renumbered,
        segmentation_type=hd.seg.SegmentationTypeValues.BINARY,
        segment_descriptions=descriptions,
        series_instance_uid=hd.UID(),
        series_number=int(getattr(datasets[0], "SeriesNumber", 0) or 0) + 1000,
        sop_instance_uid=hd.UID(),
        instance_number=1,
        manufacturer="XNATWorks",
        manufacturer_model_name=model_name[:64],
        software_versions=model_version[:64],
        device_serial_number="xnat-seg-wrapup",
        series_description=(series_description or f"{model_name} segmentation")[:64],
        omit_empty_frames=True,
    )
    unshare_segment_identification(segmentation)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    segmentation.save_as(str(out_path))
    logger.info("wrote DICOM SEG %s with %d segments", out_path, len(present))
    return {"file": out_path.name, "segments": mapping, "source_series": str(datasets[0].SeriesInstanceUID)}
