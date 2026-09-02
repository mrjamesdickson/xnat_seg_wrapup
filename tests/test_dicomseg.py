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
