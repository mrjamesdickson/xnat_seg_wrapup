"""Plan D27: every record carries a copy of its card, the certificate of the run. The adopt tool
embeds the bundle as XNW_CARD_BUNDLE (base64 tar); the wrapups lay it out under card/ with a
card.json summary, and card/**/* rides onto PROVENANCE."""
import base64
import io
import json
import logging
import tarfile

import pytest

from segwrapup import __version__
from segwrapup.card import BundleError, MAX_BUNDLE_BYTES, unpack_bundle, write_card_copy
from segwrapup.proc import PROC_DEFAULT_RESOURCES
from segwrapup.publish import DEFAULT_RESOURCES, RecordContract, collect_files

CARD_FILES = {"metadata.json": json.dumps({"id": "mriqc", "version": "1.4.0", "licences": []}).encode(),
              "README.md": b"# mriqc\n", "command.json": b"{}", "LICENSE": b"MIT\n"}


def bundle(files=CARD_FILES, gz=True, names=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(names.get(name, name) if names else name); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


ENV = {"XNW_CARD_ID": "mriqc", "XNW_CARD_REVISION": "1.4.0", "XNW_CARD_URL": "https://registry.example/cards/mriqc/1.4.0",
       "XNW_CONTAINER_IMAGE": "nipreps/mriqc:24.0.2", "XNW_CONTAINER_DIGEST": "sha256:abc"}


def test_the_bundle_is_laid_out_under_card_with_a_summary(tmp_path):
    summary = write_card_copy(tmp_path, "proc-wrapup", environ={**ENV, "XNW_CARD_BUNDLE": bundle()})
    assert sorted(p.name for p in (tmp_path / "card").iterdir()) == ["LICENSE", "README.md", "card.json", "command.json", "metadata.json"]
    assert json.loads((tmp_path / "card" / "metadata.json").read_text())["id"] == "mriqc"
    written = json.loads((tmp_path / "card" / "card.json").read_text())
    assert written == summary
    assert summary["card_id"] == "mriqc" and summary["card_revision"] == "1.4.0" and summary["card_url"] == ENV["XNW_CARD_URL"]
    assert summary["container_image"] == "nipreps/mriqc:24.0.2" and summary["container_digest"] == "sha256:abc"
    assert summary["wrapup"] == "proc-wrapup" and summary["wrapup_version"] == __version__
    assert summary["bundle_files"] == ["LICENSE", "README.md", "command.json", "metadata.json"] and len(summary["bundle_sha256"]) == 64
    assert "bundle_error" not in summary
    # a plain (uncompressed) tar is accepted too, and a subdirectory keeps its path
    plain = write_card_copy(tmp_path / "plain", "seg-wrapup", environ={"XNW_CARD_BUNDLE": bundle(gz=False, names={"README.md": "docs/README.md"})})
    assert (tmp_path / "plain" / "card" / "docs" / "README.md").read_bytes() == b"# mriqc\n" and "docs/README.md" in plain["bundle_files"]


def test_without_a_bundle_the_record_still_gets_card_json(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        summary = write_card_copy(tmp_path, "record-fetch", environ=ENV, extra={"stage": "setup"})
    assert [p.name for p in (tmp_path / "card").iterdir()] == ["card.json"]
    assert summary["bundle_files"] == [] and summary["bundle_sha256"] == "" and summary["stage"] == "setup"
    assert "no XNW_CARD_BUNDLE in the environment" in caplog.text


@pytest.mark.parametrize("encoded, reason", [
    ("not base64!!", "is not base64"),
    (base64.b64encode(b"just bytes").decode(), "is not a tar archive"),
    (bundle(names={"README.md": "../escape.md"}), "not a regular file at a safe relative path"),
    (bundle(names={"README.md": "/etc/passwd"}), "not a regular file at a safe relative path"),
    (bundle(files={}), "holds no files"),
])
def test_a_malformed_bundle_is_refused_with_the_reason(encoded, reason, tmp_path):
    with pytest.raises(BundleError, match=reason):
        unpack_bundle(encoded, tmp_path / "card")
    assert not list((tmp_path / "card").rglob("*")) or not (tmp_path / "card" / "escape.md").exists()
    assert not (tmp_path / "escape.md").exists()


def test_an_oversized_bundle_is_refused(tmp_path):
    big = bundle(files={"blob.bin": b"\0" * (MAX_BUNDLE_BYTES + 1)})
    with pytest.raises(BundleError, match="a card bundle is at most|unpacks past"):
        unpack_bundle(big, tmp_path / "card")


def test_a_malformed_bundle_is_logged_recorded_in_card_json_and_never_fatal(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        summary = write_card_copy(tmp_path, "proc-wrapup", environ={**ENV, "XNW_CARD_BUNDLE": "%%%"})
    assert "card copy not laid out for mriqc 1.4.0" in caplog.text
    assert summary["bundle_error"].startswith("XNW_CARD_BUNDLE is not base64")
    assert json.loads((tmp_path / "card" / "card.json").read_text())["bundle_error"] == summary["bundle_error"]


def test_the_card_copy_rides_onto_provenance_for_both_wrapups(tmp_path):
    """card/**/* is in both PROVENANCE defaults, so the copy is uploaded with wrapup.json and is
    never mistaken for the tool's output."""
    for defaults, derived_root in ((PROC_DEFAULT_RESOURCES, "raw"), (DEFAULT_RESOURCES, None)):
        root = tmp_path / ("proc" if derived_root else "seg"); root.mkdir()
        (root / "wrapup.json").write_text("{}")
        if derived_root:
            (root / derived_root).mkdir(); (root / derived_root / "dataset_description.json").write_text("{}")
        else:
            (root / "mask.nii.gz").write_bytes(b"x")
        write_card_copy(root, "proc-wrapup", environ={**ENV, "XNW_CARD_BUNDLE": bundle(names={"README.md": "docs/README.md"})})
        contract = RecordContract.from_env({"XNW_CARD_ID": "mriqc"}, defaults=defaults)
        files = collect_files(root, contract, derived_root=derived_root)
        provenance = sorted(f.name for f in files["PROVENANCE"])
        assert provenance == ["card/LICENSE", "card/card.json", "card/command.json", "card/docs/README.md", "card/metadata.json", "wrapup.json"], provenance
        assert not [f.name for f in files["DERIVED"] if f.name.startswith("card/")]
