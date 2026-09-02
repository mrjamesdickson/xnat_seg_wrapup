import json

from segwrapup import labels


def test_nnunet_v2_dataset_json_is_name_to_int(tmp_path):
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps({"labels": {"background": 0, "spleen": 1, "kidney_right": 2, "region": [1, 2]}}))
    assert labels.load_labels(path) == {0: "background", 1: "spleen", 2: "kidney_right"}


def test_nnunet_v1_dataset_json_is_int_to_name(tmp_path):
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps({"labels": {"0": "background", "1": "liver"}}))
    assert labels.load_labels(path) == {0: "background", 1: "liver"}


def test_monai_metadata_channel_def(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"network_data_format": {"outputs": {"pred": {"channel_def": {"0": "background", "1": "spleen"}}}}}))
    assert labels.load_labels(path) == {0: "background", 1: "spleen"}


def test_itksnap_round_trip(tmp_path):
    table = {1: "spleen", 4: 'kidney "left"'}
    path = tmp_path / "labels.txt"
    path.write_text(labels.itksnap_label_file(table))
    assert labels.load_labels(path) == {1: "spleen", 4: "kidney 'left'"}


def test_csv_with_header(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text("label,name\n1,spleen\n2,liver\n")
    assert labels.load_labels(path) == {1: "spleen", 2: "liver"}


def test_generic_json_either_orientation(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"1": "spleen"}))
    b = tmp_path / "b.json"
    b.write_text(json.dumps({"spleen": 1}))
    assert labels.load_labels(a) == labels.load_labels(b) == {1: "spleen"}


def test_unsupported_and_corrupt_files_raise(tmp_path):
    bad = tmp_path / "labels.json"
    bad.write_text("{not json")
    try:
        labels.load_labels(bad)
    except ValueError as error:
        assert "cannot read" in str(error)
    else:
        raise AssertionError("corrupt JSON should raise")
    try:
        labels.load_labels(tmp_path / "labels.yaml")
    except ValueError as error:
        assert "unsupported" in str(error)
    else:
        raise AssertionError("unknown extension should raise")


def test_discover_prefers_labels_json_over_dataset_json(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "dataset.json").write_text(json.dumps({"labels": {"spleen": 1}}))
    (tmp_path / "labels.json").write_text(json.dumps({"1": "liver"}))
    table, source = labels.discover_labels(tmp_path)
    assert table == {1: "liver"}
    assert source == tmp_path / "labels.json"


def test_discover_returns_empty_when_nothing_found(tmp_path):
    assert labels.discover_labels(tmp_path) == ({}, None)


def test_colours_are_stable_and_distinct():
    assert labels.label_color(1) == labels.label_color(1)
    assert len({labels.label_color(i) for i in range(1, 105)}) == 104


def test_slicer_table_and_collect_labels():
    declared = {0: "background", 1: "spleen", 2: "liver"}
    results = [{"structures": [{"label": 2, "name": "liver"}, {"label": 9, "name": "label 9"}]}]
    table = labels.collect_labels(declared, results)
    assert table == {1: "spleen", 2: "liver", 9: "label 9"}
    ctbl = labels.slicer_color_table({1: "kidney left"})
    assert "1 kidney_left" in ctbl and ctbl.startswith("# Color table file")


def test_bids_dseg_tsv_has_index_name_hex_colour_and_skips_background():
    text = labels.bids_dseg_tsv({0: "background", 3: "spleen", 1101: "Left Levator Scapulae"})
    lines = text.splitlines()
    red, green, blue = labels.label_color(3)
    assert lines[0] == "index\tname\tcolor"
    assert lines[1] == f"3\tspleen\t#{red:02x}{green:02x}{blue:02x}"
    assert lines[2].startswith("1101\tLeft Levator Scapulae\t#")
    assert len(lines) == 3


def test_bids_dseg_tsv_colour_source_keeps_original_colour_for_renumbered_index():
    original = labels.label_color(1101)
    text = labels.bids_dseg_tsv({1: "Left Levator Scapulae"}, colour_source={1: 1101})
    assert text.splitlines()[1] == "1\tLeft Levator Scapulae\t#%02x%02x%02x" % original
    assert labels.label_color(1) != original  # control: the index's own colour would differ
