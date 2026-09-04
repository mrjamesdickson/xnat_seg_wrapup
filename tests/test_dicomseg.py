import logging

import nibabel as nib
import numpy as np
import pytest

from segwrapup import dicomseg
from tests.conftest import COLS, ROWS, SLICES, blob_mask, series_ras_affine, write_mask


def test_load_series_sorts_by_position_not_filename(ct_series):
    directory, _ = ct_series
    datasets = dicomseg.load_series(directory)
    positions = [float(ds.ImagePositionPatient[2]) for ds in datasets]
    assert positions == sorted(positions)
    assert len(datasets) == SLICES


def test_load_series_ignores_non_dicom_and_empty_dir(ct_series, tmp_path):
    directory, _ = ct_series
    (directory / "README.txt").write_text("not dicom")
    assert len(dicomseg.load_series(directory)) == SLICES
    with pytest.raises(ValueError, match="no DICOM image instances"):
        dicomseg.load_series(tmp_path / "empty")


def test_series_affine_matches_dcm2niix_geometry(ct_series):
    _, datasets = ct_series
    datasets = sorted(datasets, key=lambda ds: ds.ImagePositionPatient[2])
    assert np.allclose(dicomseg.series_affine_ras(datasets), series_ras_affine())


def test_align_identity_geometry(ct_series, tmp_path):
    directory, _ = ct_series
    mask = write_mask(tmp_path / "mask.nii.gz", blob_mask(), affine=series_ras_affine())
    frames = dicomseg.align_mask_to_series(nib.load(str(mask)), dicomseg.load_series(directory))
    assert frames.shape == (SLICES, ROWS, COLS)
    assert (frames[2:5, 3:7, 5:10] == 3).all()
    assert frames[0, 0, 0] == 7
    assert int((frames == 3).sum()) == 5 * 4 * 3


def test_align_flipped_and_permuted_mask_gives_same_frames(ct_series, tmp_path):
    """A mask saved with x flipped and axes stored (slice,row,col) must land identically."""
    directory, _ = ct_series
    data = blob_mask()
    affine = series_ras_affine()
    # Flip x: reverse the first axis and move the origin to the other end.
    flipped = data[::-1, :, :]
    flipped_affine = affine.copy()
    flipped_affine[0, 0] = -affine[0, 0]
    flipped_affine[0, 3] = affine[0, 3] + affine[0, 0] * (COLS - 1)
    # Permute to (slice, row, col) storage order.
    permuted = np.transpose(flipped, (2, 1, 0))
    permuted_affine = flipped_affine[:, [2, 1, 0, 3]]
    mask = write_mask(tmp_path / "weird.nii.gz", permuted, affine=permuted_affine)

    frames = dicomseg.align_mask_to_series(nib.load(str(mask)), dicomseg.load_series(directory))
    reference = write_mask(tmp_path / "ref.nii.gz", data, affine=affine)
    expected = dicomseg.align_mask_to_series(nib.load(str(reference)), dicomseg.load_series(directory))
    assert np.array_equal(frames, expected)


def test_align_refuses_wrong_grid(ct_series, tmp_path):
    directory, _ = ct_series
    mask = write_mask(tmp_path / "small.nii.gz", np.zeros((COLS, ROWS, SLICES + 1)), affine=series_ras_affine())
    with pytest.raises(ValueError, match="refusing to resample"):
        dicomseg.align_mask_to_series(nib.load(str(mask)), dicomseg.load_series(directory))


def test_align_warns_on_shifted_origin(ct_series, tmp_path, caplog):
    directory, _ = ct_series
    affine = series_ras_affine()
    affine[2, 3] += 5.0
    mask = write_mask(tmp_path / "shift.nii.gz", blob_mask(), affine=affine)
    with caplog.at_level(logging.WARNING):
        dicomseg.align_mask_to_series(nib.load(str(mask)), dicomseg.load_series(directory))
    assert "origin differs" in caplog.text


def test_write_dicom_seg_round_trip(ct_series, tmp_path, read_dicom):
    directory, _ = ct_series
    mask = write_mask(tmp_path / "mask.nii.gz", blob_mask(), affine=series_ras_affine())
    out = tmp_path / "out" / "segmentation.seg.dcm"
    info = dicomseg.write_dicom_seg(
        mask, directory, {3: "spleen", 7: "marker"}, out, model_name="spleen_ct_segmentation", model_version="0.5.7"
    )
    assert info["segments"] == {1: 3, 2: 7}
    seg = read_dicom(out)
    assert str(seg.SOPClassUID) == dicomseg.SEG_SOP_CLASS_UID
    assert seg.Modality == "SEG"
    assert [s.SegmentLabel for s in seg.SegmentSequence] == ["spleen", "marker"]
    assert seg.SegmentSequence[0].SegmentAlgorithmType == "AUTOMATIC"
    assert seg.SegmentSequence[0].SegmentationAlgorithmIdentificationSequence[0].AlgorithmName == "spleen_ct_segmentation"
    assert seg.FrameOfReferenceUID == read_dicom(next(directory.glob("*.dcm"))).FrameOfReferenceUID
    assert seg.ManufacturerModelName == "spleen_ct_segmentation"
    # 3 slices carry the blob plus 1 slice carries the marker: empty frames are omitted.
    assert int(seg.NumberOfFrames) == 3 + 1

    import highdicom as hd

    parsed = hd.seg.segread(str(out))
    volume = parsed.get_volume(combine_segments=True, relabel=False)
    array = volume.array
    assert (array == 1).sum() == 5 * 4 * 3
    assert (array == 2).sum() == 1


def test_write_dicom_seg_refuses_empty_mask(ct_series, tmp_path):
    directory, _ = ct_series
    mask = write_mask(tmp_path / "empty.nii.gz", np.zeros((COLS, ROWS, SLICES)), affine=series_ras_affine())
    with pytest.raises(ValueError, match="empty"):
        dicomseg.write_dicom_seg(mask, directory, {}, tmp_path / "x.dcm", model_name="m")


def _strip_patient_study_attributes(directory, read_dicom):
    """Re-save every slice without the Type 2 patient/study attributes, as TCIA de-identification does."""
    for path in directory.glob("*.dcm"):
        ds = read_dicom(path)
        for name in dicomseg._TYPE2_PATIENT_STUDY_ATTRIBUTES:
            if name in ds:
                delattr(ds, name)
        ds.save_as(str(path), enforce_file_format=True)


def test_write_dicom_seg_tolerates_deidentified_series_missing_type2_attributes(ct_series, tmp_path, read_dicom):
    directory, _ = ct_series
    _strip_patient_study_attributes(directory, read_dicom)
    assert "PatientBirthDate" not in read_dicom(next(directory.glob("*.dcm")))  # control: really stripped
    mask = write_mask(tmp_path / "mask.nii.gz", blob_mask(), affine=series_ras_affine())
    out = tmp_path / "out" / "segmentation.seg.dcm"

    info = dicomseg.write_dicom_seg(mask, directory, {3: "spleen"}, out, model_name="m", model_version="1")

    assert info["segments"] == {1: 3, 2: 7}
    seg = read_dicom(out)
    assert seg.Modality == "SEG"
    assert seg.PatientBirthDate == ""
    assert seg.PatientID == ""
    assert int(seg.NumberOfFrames) == 3 + 1


def test_ensure_type2_attributes_reports_missing_and_keeps_present_values(ct_series):
    _, datasets = ct_series
    for ds in datasets:
        delattr(ds, "PatientBirthDate")
        delattr(ds, "AccessionNumber")

    missing = dicomseg.ensure_type2_patient_study_attributes(datasets)

    assert missing == ["PatientBirthDate", "AccessionNumber"]
    assert all(ds.PatientBirthDate == "" and ds.AccessionNumber == "" for ds in datasets)
    assert all(ds.PatientID == "WRAPUP001" for ds in datasets)


def test_write_dicom_seg_sets_recommended_display_colour_per_segment(ct_series, tmp_path, read_dicom):
    directory, _ = ct_series
    mask = write_mask(tmp_path / "mask.nii.gz", blob_mask(), affine=series_ras_affine())
    out = tmp_path / "out" / "segmentation.seg.dcm"

    dicomseg.write_dicom_seg(mask, directory, {3: "spleen", 7: "marker"}, out, model_name="m")

    seg = read_dicom(out)
    colours = [list(s.RecommendedDisplayCIELabValue) for s in seg.SegmentSequence]
    assert len(colours) == 2 and colours[0] != colours[1]
    from highdicom.color import CIELabColor
    from segwrapup.labels import label_color
    expected = CIELabColor.from_rgb(*label_color(3)).value
    assert colours[0] == list(expected)


def test_single_segment_seg_carries_segment_identification_per_frame(ct_series, tmp_path, read_dicom):
    """XNAT's ROI plugin reads SegmentIdentificationSequence from the per-frame groups only.

    highdicom hoists a functional group into SharedFunctionalGroupsSequence when its value
    is constant across every frame, which for a one-label mask it always is. The result is
    valid DICOM that XNAT rejects with HTTP 500 "SegmentIdentification missing", so every
    single-lesion model (DeepWMH and the rest of the disease queue) failed to register its
    ROI collection while multi-structure models were unaffected.
    """
    directory, _ = ct_series
    lesion = np.zeros((COLS, ROWS, SLICES))
    lesion[3:7, 3:7, 2:5] = 1
    mask = write_mask(tmp_path / "lesion.nii.gz", lesion, affine=series_ras_affine())
    out = tmp_path / "single.seg.dcm"
    dicomseg.write_dicom_seg(mask, directory, {1: "white matter hyperintensity"}, out, model_name="DeepWMH")

    seg = read_dicom(out)
    assert len(seg.SegmentSequence) == 1
    assert "SegmentIdentificationSequence" not in seg.SharedFunctionalGroupsSequence[0], (
        "the macro must not remain in the shared groups; DICOM forbids it in both places"
    )
    for frame in seg.PerFrameFunctionalGroupsSequence:
        assert frame.SegmentIdentificationSequence[0].ReferencedSegmentNumber == 1


def test_multi_segment_seg_keeps_per_frame_segment_identification(ct_series, tmp_path, read_dicom):
    """Control: the multi-segment path already put the macro per-frame and must not change."""
    directory, _ = ct_series
    mask = write_mask(tmp_path / "mask.nii.gz", blob_mask(), affine=series_ras_affine())
    out = tmp_path / "multi.seg.dcm"
    dicomseg.write_dicom_seg(mask, directory, {3: "spleen", 7: "marker"}, out, model_name="m")

    seg = read_dicom(out)
    assert "SegmentIdentificationSequence" not in seg.SharedFunctionalGroupsSequence[0]
    referenced = {f.SegmentIdentificationSequence[0].ReferencedSegmentNumber for f in seg.PerFrameFunctionalGroupsSequence}
    assert referenced == {1, 2}
