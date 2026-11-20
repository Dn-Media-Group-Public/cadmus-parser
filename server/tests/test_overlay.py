"""The overlay generator and the static stack file."""

import json
import os

import yaml

from cadmus_parser_server import overlay

STACK = os.path.join(os.path.dirname(__file__), "..", "..", "docker-compose.parse.yml")


def test_overlay_per_consumer_networks_env_and_optional_parts():
    doc = overlay.overlay(["a-web", "b-web"], dev_store="seaweedfs:8333", ocr=True, parser_image="me/parser:1", extra_env={"a-web": {"X": "1"}})
    assert doc["services"]["parser"] == {"networks": ["a-web-parse", "b-web-parse"], "image": "me/parser:1"}
    assert doc["services"]["a-web"]["networks"] == ["a-web-parse"] and doc["services"]["a-web"]["environment"] == {
        "PARSE_URL": "http://parser:8080", "PARSE_TOKEN": "${WRAPPER_TOKEN}", "X": "1"}  # one network per consumer: the consumers cannot reach each other
    assert doc["networks"] == {"a-web-parse": {"driver": "overlay", "internal": True}, "b-web-parse": {"driver": "overlay", "internal": True}}
    assert doc["services"]["blob-sidecar"] == {"networks": ["seaweedfs"], "environment": {"PARSE_PROXY_ALLOW": "seaweedfs:8333"}}
    assert doc["services"]["ocr-sidecar"]["networks"] == ["parse-ocr", "public"] and "NET_ADMIN" in doc["services"]["ocr-sidecar"]["cap_add"]
    assert overlay.overlay(["w"], network_names={"w": "mine"})["services"]["w"]["networks"] == ["mine"]
    plain = overlay.overlay(["w"])
    assert set(plain["services"]) == {"parser", "w"} and "image" not in plain["services"]["parser"]


def test_cli_prints_yaml_readable_json(capsys):
    overlay.main(["--consumer", "w", "--ocr"])
    doc = yaml.safe_load(capsys.readouterr().out)
    assert doc == json.loads(json.dumps(doc)) and "ocr-sidecar" in doc["services"]


def test_static_stack_file_keeps_the_parser_off_the_internet():
    base = yaml.safe_load(open(STACK))
    assert base["services"]["blob-sidecar"]["networks"] == ["parse-egress", "public"] and "public" not in base["services"]["parser"]["networks"]
    assert all(base["networks"][n]["internal"] for n in ("parse-egress", "parse-ocr"))
    env = base["services"]["parser"]["environment"]
    assert "RESEARCH_BLOB_OPTIONS" not in env and "BLOB_TOKEN_SECRET" not in env and "RESEARCH_BLOB_URL" not in env  # no store, no secret: it all comes per job
    assert set(base["services"]["parser"]["cap_add"]) == {"SETUID", "SETGID", "CHOWN", "KILL", "SETPCAP"} and base["services"]["parser"]["cap_drop"] == ["ALL"]
