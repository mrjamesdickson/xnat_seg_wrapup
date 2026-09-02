import json

import numpy as np
import pytest

from segwrapup import cli
from tests.conftest import blob_mask, series_ras_affine, write_ct_series, write_mask

EXPECTED_FILES = {"report.html", "volumes.json", "volumes.csv", "labels.txt", "labels.ctbl", "wrapup.json"}


def run_cli(*args):
    return cli.main([str(a) for a in args])


def test_single_label_map_without_dicom(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    data = np.zeros((8, 8, 8), dtype=np.uint8)
    data[:4] = 2
    (inp / "pred").mkdir()
    write_mask(inp / "pred" / "case_seg.nii.gz", data)
    (inp / "labels.json").write_text(json.dumps({"2": "liver"}))

    assert run_cli("--input", inp, "--output", out, "--model", "demo") == 0

    assert EXPECTED_FILES | {"case_seg.nii.gz"} <= {p.name for p in out.iterdir()}
    volumes = json.loads((out / "volumes.json").read_text())
    assert volumes["model"] == "demo"
    assert volumes["results"][0]["structures"][0]["name"] == "liver"
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["merged"] is False
    assert manifest["dicom_seg"] is None
    assert manifest["label_source"].endswith("labels.json")
    assert "liver" in (out / "labels.txt").read_text()


def test_binary_set_is_merged_and_named_from_filenames(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    seg = inp / "segmentations"
    seg.mkdir(parents=True)
    for name, slab in (("spleen", 0), ("liver", 1), ("kidney_left", 2)):
        data = np.zeros((6, 6, 6), dtype=np.uint8)
        data[slab] = 1
        write_mask(seg / f"{name}.nii.gz", data)

    assert run_cli("--input", inp, "--output", out, "--model", "totalsegmentator") == 0

    names = {p.name for p in out.iterdir()}
    assert "segmentation.nii.gz" in names and "spleen.nii.gz" not in names
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["merged"] is True
    assert manifest["merged_labels"] == {"1": "kidney_left", "2": "liver", "3": "spleen"}
    csv_text = (out / "volumes.csv").read_text()
    assert "kidney_left" in csv_text and "spleen" in csv_text


def test_merge_can_be_forced_off(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    for name in ("a", "b"):
        write_mask(inp / f"{name}.nii.gz", np.ones((3, 3, 3)))
    assert run_cli("--input", inp, "--output", out, "--merge", "no") == 0
    assert {"a.nii.gz", "b.nii.gz"} <= {p.name for p in out.iterdir()}


def test_dicom_seg_written_when_source_present(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    write_ct_series(inp / ".source_dicom")
    write_mask(inp / "mask.nii.gz", blob_mask(), affine=series_ras_affine())
    (inp / "labels.json").write_text(json.dumps({"3": "spleen", "7": "marker"}))

    assert run_cli("--input", inp, "--output", out, "--model", "spleen_ct_segmentation", "--model-version", "0.5.7") == 0

    names = {p.name for p in out.iterdir()}
    assert "segmentation.seg.dcm" in names
    assert ".source_dicom" not in names  # source is consumed, never re-uploaded
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["dicom_seg"]["segments"] == {"1": 3, "2": 7}


def test_dicom_seg_failure_does_not_fail_the_run(tmp_path, caplog):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    write_ct_series(inp / ".source_dicom")
    write_mask(inp / "mask.nii.gz", np.ones((2, 2, 2)))  # wrong grid

    assert run_cli("--input", inp, "--output", out) == 0
    assert "DICOM SEG not written" in caplog.text
    assert (out / "mask.nii.gz").exists() and (out / "report.html").exists()
    assert json.loads((out / "wrapup.json").read_text())["dicom_seg"] is None


def test_no_masks_fails(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    (inp / "log.txt").write_text("model ran but wrote nothing")
    assert run_cli("--input", inp, "--output", out) == 1
    assert not (out / "report.html").exists()


def test_missing_input_dir_fails(tmp_path):
    assert run_cli("--input", tmp_path / "nope", "--output", tmp_path / "out") == 1


def test_bad_label_file_falls_back_to_numbers(tmp_path, caplog):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    write_mask(inp / "m.nii.gz", np.ones((3, 3, 3)))
    bad = tmp_path / "labels.json"
    bad.write_text("{oops")
    assert run_cli("--input", inp, "--output", out, "--labels", bad) == 0
    assert "label file rejected" in caplog.text
    assert "label 1" in (out / "volumes.csv").read_text()


def test_env_defaults_drive_paths_and_model(tmp_path, monkeypatch):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    write_mask(inp / "m.nii.gz", np.ones((3, 3, 3)))
    monkeypatch.setenv("SEG_INPUT", str(inp))
    monkeypatch.setenv("SEG_OUTPUT", str(out))
    monkeypatch.setenv("SEG_MODEL_NAME", "from-env")
    assert cli.main([]) == 0
    assert json.loads((out / "volumes.json").read_text())["model"] == "from-env"


def test_command_json_matches_dockerfile_label():
    """The CS discovers wrapup commands from the image label; keep it identical to the JSON file."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    command = json.loads((root / "commands" / "seg-wrapup.json").read_text())
    dockerfile = (root / "Dockerfile").read_text()
    label_line = next(line for line in dockerfile.splitlines() if line.startswith("LABEL org.nrg.commands="))
    label_value = label_line[len("LABEL org.nrg.commands="):].strip()
    label_json = json.loads(json.loads(label_value))  # the label is a JSON-encoded string of a JSON array
    assert label_json == [command]
    assert command["type"] == "docker-wrapup"
    assert command["mounts"] == [] and command["inputs"] == [] and command["outputs"] == []
    assert command["image"].startswith("xnatworks/seg-wrapup:")


def test_viewer_sidecars_tsv_and_uint8_companion_for_large_labels(tmp_path):
    import nibabel as nib
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    data = np.zeros((8, 8, 8), dtype=np.uint16)
    data[:4] = 7201
    data[4:6] = 1101
    nib.save(nib.Nifti1Image(data, np.eye(4)), str(inp / "image_dseg.nii.gz"))
    (inp / "labels.json").write_text(json.dumps({"1101": "Left Levator Scapulae", "7201": "Left Adductor Magnus"}))

    assert run_cli("--input", inp, "--output", out, "--model", "MuscleMap") == 0

    names = {p.name for p in out.iterdir()}
    assert {"image_dseg.tsv", "image_dseg_uint8.nii.gz", "image_dseg_uint8.tsv"} <= names
    tsv = (out / "image_dseg.tsv").read_text().splitlines()
    assert tsv[0] == "index\tname\tcolor" and tsv[1].startswith("1101\tLeft Levator Scapulae\t#")
    uint8_tsv = (out / "image_dseg_uint8.tsv").read_text().splitlines()
    assert uint8_tsv[1].startswith("1\tLeft Levator Scapulae\t#") and uint8_tsv[2].startswith("2\tLeft Adductor Magnus\t#")
    assert uint8_tsv[1].split("\t")[2] == tsv[1].split("\t")[2]  # same colour as the original value
    manifest = json.loads((out / "wrapup.json").read_text())
    assert manifest["uint8_companions"]["image_dseg_uint8.nii.gz"]["labels"] == {"1": 1101, "2": 7201}


def test_viewer_sidecars_no_companion_for_byte_labels(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    data = np.zeros((8, 8, 8), dtype=np.uint8)
    data[:4] = 2
    write_mask(inp / "case_seg.nii.gz", data)
    (inp / "labels.json").write_text(json.dumps({"2": "liver"}))

    assert run_cli("--input", inp, "--output", out) == 0

    names = {p.name for p in out.iterdir()}
    assert "case_seg.tsv" in names
    assert not any(n.endswith("_uint8.nii.gz") for n in names)  # control
    assert "uint8_companions" not in json.loads((out / "wrapup.json").read_text())
