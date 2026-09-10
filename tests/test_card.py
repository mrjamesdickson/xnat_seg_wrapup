"""Plan D27: every record carries a copy of its card, the certificate of the run. The adopt tool
puts the card's metadata into the Container Service command as command-metadata.card (plain
JSON, jsonb); the wrapups read the command back and write card/metadata.json and card/card.json,
which ride onto PROVENANCE."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from segwrapup import __version__
from segwrapup.card import card_for_run, fetch_command_card, locate_main_container, write_card_copy
from segwrapup.proc import PROC_DEFAULT_RESOURCES
from segwrapup.publish import DEFAULT_RESOURCES, RecordContract, collect_files
from segwrapup.register import XnatContext

CARD = {"id": "mriqc", "version": "1.4.0", "name": "MRIQC - MRI Quality Control", "dockerImage": "nipreps/mriqc:24.0.2",
        "imageDigest": "sha256:abc", "license": "Apache-2.0", "tags": ["qc"], "prerequisites": [{"name": "conversion", "analysisType": "bids-conversion"}],
        "url": "https://github.com/mrjamesdickson/container-workshop/tree/main/wrappers/mriqc"}
COMMANDS = {"77": {"id": 77, "name": "mriqc", "version": "1.4.0", "command-metadata": {"card": CARD}},
            "78": {"id": 78, "name": "bare", "version": "0.1.0"},
            "79": {"id": 79, "name": "odd", "command-metadata": {"card": "not an object"}}}
CONTAINERS = [{"id": 1, "workflow-id": "9001", "subtype": "docker", "command-id": 77, "wrapper-id": 88},
              {"id": 2, "workflow-id": "9001", "subtype": "docker-setup", "command-id": 5},
              {"id": 3, "workflow-id": "9002", "subtype": "docker", "command-id": 78},
              {"id": 4, "workflow-id": "9003", "subtype": "docker", "command-id": 1}, {"id": 5, "workflow-id": "9003", "subtype": "docker", "command-id": 2}]


class _CS(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"S")

    def do_DELETE(self):
        self.send_response(200); self.end_headers()

    def do_GET(self):
        if self.path == "/xapi/containers":
            body = json.dumps(CONTAINERS).encode()
        elif self.path.startswith("/xapi/commands/") and self.path.rsplit("/", 1)[-1] in COMMANDS:
            body = json.dumps(COMMANDS[self.path.rsplit("/", 1)[-1]]).encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)


@pytest.fixture
def cs():
    server = HTTPServer(("127.0.0.1", 0), _CS); threading.Thread(target=server.serve_forever, daemon=True).start()
    yield XnatContext.from_env({"XNAT_HOST": f"http://127.0.0.1:{server.server_port}", "XNAT_USER": "u", "XNAT_PASS": "p",
                                "PROC_PROJECT": "P", "PROC_SESSION_ID": "XNAT_E1"})
    server.shutdown()


def test_the_command_card_is_read_back_from_the_container_service(cs, caplog):
    assert fetch_command_card(cs, 77) == (CARD, "")
    with caplog.at_level(logging.INFO):
        assert fetch_command_card(cs, 78) == (None, "command 78 carries no command-metadata.card")
        assert fetch_command_card(cs, 79) == (None, "command 79 carries no command-metadata.card")
    assert "command 78 (bare) carries no command-metadata.card" in caplog.text
    card, why = fetch_command_card(cs, 404)
    assert card is None and why.startswith("could not read command 404")
    assert fetch_command_card(cs, None) == (None, "no command id")


def test_a_setup_finds_the_main_container_of_its_own_workflow(cs, caplog):
    """record-fetch runs before the main; every container of a launch shares the workflow id, so
    the main is the non-helper one with the setup's own workflow id."""
    assert locate_main_container(cs, "9001")["command-id"] == 77
    assert locate_main_container(cs, None) is None
    with caplog.at_level(logging.INFO):
        assert locate_main_container(cs, "9003") is None        # two mains: ambiguous, no guess
        assert locate_main_container(cs, "nope") is None
    assert "workflow 9003 has 2 main container(s)" in caplog.text
    assert card_for_run(cs, {"command-id": 77}) == (CARD, "")
    assert card_for_run(None, {"command-id": 77}) == (None, "no XNAT context")
    assert card_for_run(cs, None) == (None, "main container not found")


def test_the_copy_is_the_block_plus_what_only_the_run_knew(tmp_path):
    summary = write_card_copy(tmp_path, "proc-wrapup", CARD, command_id=77, wrapper_id=88, scope="subject")
    assert json.loads((tmp_path / "card" / "metadata.json").read_text()) == CARD
    written = json.loads((tmp_path / "card" / "card.json").read_text())
    assert written == summary
    assert summary["wrapup"] == "proc-wrapup" and summary["wrapup_version"] == __version__
    assert summary["command_id"] == 77 and summary["wrapper_id"] == 88 and summary["run_scope"] == "subject"
    assert summary["card_id"] == "mriqc" and summary["card_revision"] == "1.4.0" and summary["card_url"] == CARD["url"]
    assert "error" not in summary


def test_without_a_block_the_record_still_gets_card_json_with_the_reason(tmp_path):
    summary = write_card_copy(tmp_path, "record-fetch", None, "command 78 carries no command-metadata.card", command_id=78, extra={"stage": "setup"})
    assert [p.name for p in (tmp_path / "card").iterdir()] == ["card.json"]
    assert summary["error"] == "command 78 carries no command-metadata.card" and summary["stage"] == "setup" and summary["card_id"] == ""


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
        write_card_copy(root, "proc-wrapup", CARD, command_id=77)
        contract = RecordContract.from_env({"XNW_CARD_ID": "mriqc"}, defaults=defaults)
        files = collect_files(root, contract, derived_root=derived_root)
        assert sorted(f.name for f in files["PROVENANCE"]) == ["card/card.json", "card/metadata.json", "wrapup.json"]
        assert not [f.name for f in files["DERIVED"] if f.name.startswith("card/")]
