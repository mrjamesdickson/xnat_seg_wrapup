import json

import numpy as np
import pytest

from segwrapup import cli, labels, volumes
from tests.conftest import write_mask


def organ_indices(names):
    return json.dumps({"organ_indices": {str(i): {"name": n, "SNOMED": {"ID": "1", "name": n}} for i, n in names.items()}})


def test_organ_indices_format_is_read(tmp_path):
    path = tmp_path / "clin_CT_organs_organ_indices.json"
    path.write_text(organ_indices({1: "adrenal_gland_left", 2: "adrenal_gland_right"}))
    assert labels.load_labels(path) == {1: "adrenal_gland_left", 2: "adrenal_gland_right"}


def test_sidecar_pairs_by_shared_prefix(tmp_path):
    (tmp_path / "clin_CT_organs_organ_indices.json").write_text(organ_indices({1: "liver"}))
    (tmp_path / "clin_CT_ribs_organ_indices.json").write_text(organ_indices({1: "rib_left_1"}))
    organs = write_mask(tmp_path / "clin_CT_organs_segmentation_CT_CT.nii.gz", np.ones((2, 2, 2)))
    ribs = write_mask(tmp_path / "clin_CT_ribs_segmentation_CT_CT.nii.gz", np.ones((2, 2, 2)))
    assert labels.sidecar_labels(organs)[0] == {1: "liver"}
    assert labels.sidecar_labels(ribs)[0] == {1: "rib_left_1"}


def test_sidecar_requires_four_shared_characters(tmp_path):
    (tmp_path / "labels.json").write_text(json.dumps({"1": "x"}))
    liver = write_mask(tmp_path / "liver.nii.gz", np.ones((2, 2, 2)))
    assert labels.sidecar_labels(liver) == ({}, None)


def test_merge_label_maps_offsets_and_keeps_names(tmp_path):
    shape = (4, 4, 4)
    organs = np.zeros(shape, dtype=np.uint8)
    organs[0] = 1  # liver
    organs[1] = 3  # spleen (label 2 absent in this scan)
    ribs = np.zeros(shape, dtype=np.uint8)
    ribs[2] = 1  # rib_left_1
    ribs[1, 0, 0] = 2  # overlaps spleen voxel: later map wins
    paths = [write_mask(tmp_path / "organs.nii.gz", organs), write_mask(tmp_path / "ribs.nii.gz", ribs)]
    tables = [{1: "liver", 2: "kidney", 3: "spleen"}, {1: "rib_left_1", 2: "rib_left_2"}]

    table = volumes.merge_label_maps(paths, tables, tmp_path / "merged.nii.gz")

    merged, _ = volumes.load_label_array(tmp_path / "merged.nii.gz")
    assert table == {1: "liver", 2: "kidney", 3: "spleen", 4: "rib_left_1", 5: "rib_left_2"}
    assert (merged[0] == 1).all() and (merged[2] == 4).all()
    assert merged[1, 0, 0] == 5 and merged[1, 1, 1] == 3


def test_merge_label_maps_rejects_shape_mismatch(tmp_path):
    paths = [write_mask(tmp_path / "a.nii.gz", np.ones((2, 2, 2))), write_mask(tmp_path / "b.nii.gz", np.ones((3, 2, 2)))]
    with pytest.raises(ValueError, match="differs"):
        volumes.merge_label_maps(paths, [{1: "a"}, {1: "b"}], tmp_path / "m.nii.gz")


def _moose_layout(inp):
    seg = inp / "moose" / "S1" / "moosez-2026-09-02-21-18-31" / "segmentations"
    seg.mkdir(parents=True)
    organs = np.zeros((5, 5, 5), dtype=np.uint8)
    organs[0] = 8
    ribs = np.zeros((5, 5, 5), dtype=np.uint8)
    ribs[4] = 27
    write_mask(seg / "clin_CT_organs_segmentation_CT_CT.nii.gz", organs)
    write_mask(seg / "clin_CT_ribs_segmentation_CT_CT.nii.gz", ribs)
    (seg / "clin_CT_organs_organ_indices.json").write_text(organ_indices({8: "liver", 15: "spleen"}))
    (seg / "clin_CT_ribs_organ_indices.json").write_text(organ_indices({1: "rib_left_1", 27: "sternum"}))
    return seg


def test_cli_merges_multi_model_moose_output(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    _moose_layout(inp)

    assert cli.main(["--input", str(inp), "--output", str(out), "--model", "MOOSE"]) == 0

    names = {p.name for p in out.iterdir()}
    assert "segmentation.nii.gz" in names
    assert not any(n.startswith("clin_CT") for n in names)
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["merged"] is True and len(manifest["merged_label_maps"]) == 2
    volumes_json = json.loads((out / "volumes.json").read_text())
    found = {s["name"] for s in volumes_json["results"][0]["structures"]}
    assert found == {"liver", "sternum"}
    label_text = (out / "labels.txt").read_text()
    assert "spleen" in label_text and "rib_left_1" in label_text  # declared-but-absent labels keep the colour map stable


def test_cli_single_moose_model_uses_its_sidecar(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    seg = inp / "segmentations"
    seg.mkdir(parents=True)
    organs = np.zeros((3, 3, 3), dtype=np.uint8)
    organs[0] = 8
    write_mask(seg / "clin_CT_organs_segmentation_CT_CT.nii.gz", organs)
    (seg / "clin_CT_organs_organ_indices.json").write_text(organ_indices({8: "liver"}))

    assert cli.main(["--input", str(inp), "--output", str(out)]) == 0
    assert "liver" in (out / "volumes.csv").read_text()
    assert json.loads((out / "wrapup.json").read_text())["merged"] is False


def test_cli_merge_no_keeps_maps_separate(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    _moose_layout(inp)
    assert cli.main(["--input", str(inp), "--output", str(out), "--merge", "no"]) == 0
    assert "segmentation.nii.gz" not in {p.name for p in out.iterdir()}
