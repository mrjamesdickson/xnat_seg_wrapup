import numpy as np
import pytest

from segwrapup import volumes
from tests.conftest import write_mask


def test_measure_mask_converts_voxels_to_millilitres(tmp_path):
    data = np.zeros((20, 20, 20), dtype=np.uint8)
    data[:10, :10, :10] = 1
    mask = write_mask(tmp_path / "cube.nii.gz", data, voxel_size=(2.0, 2.0, 2.0))
    result = volumes.measure_mask(mask, {1: "spleen"})
    assert result["structures"] == [{"label": 1, "name": "spleen", "voxels": 1000, "volume_ml": pytest.approx(8.0)}]
    assert result["voxel_size_mm"] == [2.0, 2.0, 2.0]


def test_measure_sorts_by_volume_and_names_unknown_labels(tmp_path):
    data = np.zeros((10, 10, 10), dtype=np.uint8)
    data[:2] = 1
    data[2:8] = 2
    mask = write_mask(tmp_path / "multi.nii", data)
    result = volumes.measure_mask(mask, {1: "liver"})
    assert [item["name"] for item in result["structures"]] == ["label 2", "liver"]
    assert result["total_volume_ml"] == pytest.approx(0.8)


def test_measure_rejects_non_integer_masks(tmp_path):
    import nibabel as nib

    nib.save(nib.Nifti1Image(np.random.rand(4, 4, 4).astype(np.float32), np.eye(4)), str(tmp_path / "prob.nii.gz"))
    with pytest.raises(ValueError, match="not integer"):
        volumes.measure_mask(tmp_path / "prob.nii.gz", {})


def test_find_masks_skips_excluded_and_non_nifti(tmp_path):
    (tmp_path / "a.nii.gz").write_bytes(b"")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "b.nii").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".source_dicom").mkdir()
    (tmp_path / ".source_dicom" / "c.nii.gz").write_bytes(b"")
    found = volumes.find_masks(tmp_path, exclude=(tmp_path / ".source_dicom",))
    assert [p.name for p in found] == ["a.nii.gz", "b.nii"]


def test_merge_binary_masks_uses_declared_labels_then_sequential(tmp_path):
    shape = (6, 6, 6)
    spleen = np.zeros(shape, dtype=np.uint8)
    spleen[:2] = 1
    liver = np.zeros(shape, dtype=np.uint8)
    liver[2:4] = 1
    liver[1] = 1  # overlaps spleen on one slab
    aorta = np.zeros(shape, dtype=np.uint8)
    aorta[5] = 1
    paths = [
        write_mask(tmp_path / "spleen.nii.gz", spleen),
        write_mask(tmp_path / "liver.nii.gz", liver),
        write_mask(tmp_path / "aorta.nii.gz", aorta),
    ]
    assert volumes.looks_like_binary_set(paths)
    table = volumes.merge_binary_masks(paths, tmp_path / "merged.nii.gz", {1: "spleen", 5: "liver"})
    assert table == {1: "spleen", 5: "liver", 6: "aorta"}
    merged, _ = volumes.load_label_array(tmp_path / "merged.nii.gz")
    assert set(np.unique(merged).tolist()) == {0, 1, 5, 6}
    assert (merged[1] == 1).all()  # sorted order is aorta, liver, spleen: spleen is applied last and wins


def test_merge_order_is_sorted_filename(tmp_path):
    # Sorted order is aorta, liver, spleen: spleen is applied last and wins its overlap with liver.
    shape = (4, 4, 4)
    a = np.zeros(shape, dtype=np.uint8); a[0] = 1
    b = np.zeros(shape, dtype=np.uint8); b[0] = 1
    paths = [write_mask(tmp_path / "liver.nii.gz", a), write_mask(tmp_path / "spleen.nii.gz", b)]
    table = volumes.merge_binary_masks(paths, tmp_path / "m.nii.gz", {})
    merged, _ = volumes.load_label_array(tmp_path / "m.nii.gz")
    assert table == {1: "liver", 2: "spleen"}
    assert (merged[0] == 2).all()


def test_merge_refuses_mismatched_shapes(tmp_path):
    paths = [
        write_mask(tmp_path / "a.nii.gz", np.ones((4, 4, 4))),
        write_mask(tmp_path / "b.nii.gz", np.ones((4, 4, 5))),
    ]
    with pytest.raises(ValueError, match="differs"):
        volumes.merge_binary_masks(paths, tmp_path / "m.nii.gz")


def test_single_multilabel_file_is_not_a_binary_set(tmp_path):
    data = np.zeros((4, 4, 4), dtype=np.uint8)
    data[0] = 1
    data[1] = 2
    paths = [write_mask(tmp_path / "seg.nii.gz", data), write_mask(tmp_path / "other.nii.gz", data)]
    assert not volumes.looks_like_binary_set(paths)
    assert not volumes.looks_like_binary_set(paths[:1])


def _write_uint16_mask(path, array):
    import nibabel as nib
    nib.save(nib.Nifti1Image(array.astype(np.uint16), np.eye(4)), str(path))
    return path


def test_uint8_companion_needed_only_above_255(tmp_path):
    small = write_mask(tmp_path / "small.nii.gz", np.full((4, 4, 4), 7, dtype=np.uint8))
    big = _write_uint16_mask(tmp_path / "big.nii.gz", np.full((4, 4, 4), 1101))
    assert volumes.needs_uint8_companion(small) is False
    assert volumes.needs_uint8_companion(big) is True


def test_uint8_companion_renumbers_sorted_present_labels(tmp_path):
    data = np.zeros((6, 6, 6), dtype=np.uint16)
    data[:2] = 7201
    data[2:4] = 1101
    data[4:5] = 6121
    source = _write_uint16_mask(tmp_path / "image_dseg.nii.gz", data)

    mapping = volumes.write_uint8_companion(source, tmp_path / "image_dseg_uint8.nii.gz")

    assert mapping == {1: 1101, 2: 6121, 3: 7201}
    out, image = volumes.load_label_array(tmp_path / "image_dseg_uint8.nii.gz")
    assert image.get_data_dtype() == np.uint8
    assert sorted(int(v) for v in np.unique(out)) == [0, 1, 2, 3]
    assert (out[:2] == 3).all() and (out[2:4] == 1).all() and (out[4:5] == 2).all()


def test_uint8_companion_refuses_more_than_255_labels(tmp_path):
    data = np.arange(1, 257, dtype=np.uint16).reshape(16, 16, 1) * 10
    source = _write_uint16_mask(tmp_path / "many.nii.gz", data)
    with pytest.raises(ValueError, match="more than a byte"):
        volumes.write_uint8_companion(source, tmp_path / "many_uint8.nii.gz")
