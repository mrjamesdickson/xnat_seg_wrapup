from segwrapup.report import DISCLAIMER, render_html


def sample_report(structures):
    return {
        "model": "wholeBody_ct_segmentation",
        "generated": "2026-09-02 12:00 UTC",
        "session": "XNAT_E00950",
        "scan": "2",
        "results": [
            {
                "file": "segmentation.nii.gz",
                "shape": [512, 512, 300],
                "voxel_size_mm": [0.78, 0.78, 1.5],
                "voxel_volume_mm3": 0.9126,
                "structures": structures,
                "total_volume_ml": sum(s["volume_ml"] for s in structures),
            }
        ],
    }


def test_html_is_self_contained_and_escaped():
    html = render_html(sample_report([{"label": 1, "name": "spleen <b>", "voxels": 100, "volume_ml": 302.9}]))
    assert html.startswith("<!DOCTYPE html>")
    assert "spleen &lt;b&gt;" in html
    assert "302.90" in html and "302.9 mL" in html
    assert "XNAT_E00950" in html and "scan <strong>2</strong>" in html
    assert DISCLAIMER in html
    assert "http" not in html.split("<body>")[1]  # no external assets


def test_many_structures_render_one_row_each_with_bars():
    structures = [{"label": i, "name": f"s{i}", "voxels": i * 10, "volume_ml": float(i)} for i in range(1, 105)]
    html = render_html(sample_report(structures))
    assert html.count("<tr><td class='name'>") == 104
    assert html.count("class='chip'") == 104
    assert "width:100.0%" in html  # largest structure fills the bar
    assert "width:1.0%" in html  # smallest is visible, not zero


def test_empty_mask_says_so():
    html = render_html(sample_report([]))
    assert "No labelled structures" in html
    assert "0.0 mL" in html
