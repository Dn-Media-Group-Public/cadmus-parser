"""The stack file overlay for one client stack: `python -m cadmus_parser_server.overlay --client web --client worker [--dev-store seaweedfs:8333]
[--ocr] [--asr] [--caption] [--parser-image myorg/my-parser:1] > parse-overlay.yml`, deployed after docker-compose.parse.yml (`docker stack deploy -c … -c …`).

It adds what depends on the client: a network `<client>-parse` per client service that only that client and the parser are on (one shared
network would let the clients reach each other), PARSE_URL and PARSE_TOKEN (= ${WRAPPER_TOKEN}) in each client, the parser on all of those
networks, the client's own image for `parser`, the dev store's network for the egress proxy, and the `ocr-sidecar` with OCR (`asr-sidecar` with ASR, `caption-sidecar` with caption). The output is JSON,
which is YAML; a client's own per-service additions go in `extra_env`."""

import argparse
import json


def overlay(clients: list[str], dev_store: str | None = None, ocr: bool = False, asr: bool = False, caption: bool = False, parser_image: str | None = None, extra_env: dict | None = None,
            dev_store_network: str | None = None, network_names: dict | None = None) -> dict:
    """`dev_store` = host:port of a store inside the stack that the proxy may reach (exactly that address and port); its network is `dev_store_network`
    (default: the host name). `extra_env` = {service: {VAR: value}} merged into the clients' environment. `network_names` = {service: network}
    overrides the default network name `<service>-parse`."""
    nets = {c: (network_names or {}).get(c) or f"{c}-parse" for c in clients}
    doc: dict = {"version": "3.8", "services": {"parser": {"networks": list(nets.values()), **({"image": parser_image} if parser_image else {})}},
                 "networks": {n: {"driver": "overlay", "internal": True} for n in nets.values()}}
    for c, n in nets.items():
        doc["services"][c] = {"networks": [n], "environment": {"PARSE_URL": "http://parser:8080", "PARSE_TOKEN": "${WRAPPER_TOKEN}", **((extra_env or {}).get(c) or {})}}
    if dev_store:
        doc["services"]["blob-sidecar"] = {"networks": [dev_store_network or dev_store.split(":")[0]], "environment": {"PARSE_PROXY_ALLOW": dev_store}}
    if ocr:
        doc["services"]["ocr-sidecar"] = {
            "image": "${IMAGE_PREFIX:-}cadmus-parser-ocr-sidecar:${IMAGE_TAG:-local}", "cap_drop": ["ALL"],
            "cap_add": ["NET_ADMIN", "SETUID", "SETGID", "SETPCAP", "KILL"], "deploy": {"replicas": 1, "restart_policy": {"condition": "any"}},
            "environment": {"OCR_PROFILES": "${OCR_PROFILES}", "OCR_TIMEOUT": "${OCR_TIMEOUT:-120}", "OCR_MAX_BODY": "${OCR_MAX_BODY:-10485760}",
                            "OCR_MAX_CONCURRENT": "${OCR_MAX_CONCURRENT:-4}"}, "networks": ["parse-ocr", "public"]}
    if asr:
        doc["services"]["asr-sidecar"] = {
            "image": "${IMAGE_PREFIX:-}cadmus-parser-asr-sidecar:${IMAGE_TAG:-local}", "cap_drop": ["ALL"],
            "cap_add": ["NET_ADMIN", "SETUID", "SETGID", "SETPCAP", "KILL"], "deploy": {"replicas": 1, "restart_policy": {"condition": "any"}},
            "environment": {"ASR_PROFILES": "${ASR_PROFILES}", "ASR_ADMIN_TOKEN": "${ASR_ADMIN_TOKEN:-}", "ASR_TIMEOUT": "${ASR_TIMEOUT:-600}",
                            "ASR_MAX_BODY": "${ASR_MAX_BODY:-26214400}", "ASR_MAX_CONCURRENT": "${ASR_MAX_CONCURRENT:-4}"}, "networks": ["parse-asr", "public"]}
    if caption:
        doc["services"]["caption-sidecar"] = {
            "image": "${IMAGE_PREFIX:-}cadmus-parser-caption-sidecar:${IMAGE_TAG:-local}", "cap_drop": ["ALL"],
            "cap_add": ["NET_ADMIN", "SETUID", "SETGID", "SETPCAP", "KILL"], "deploy": {"replicas": 1, "restart_policy": {"condition": "any"}},
            "environment": {"CAPTION_PROFILES": "${CAPTION_PROFILES}", "CAPTION_ADMIN_TOKEN": "${CAPTION_ADMIN_TOKEN:-}", "CAPTION_TIMEOUT": "${CAPTION_TIMEOUT:-120}",
                            "CAPTION_MAX_BODY": "${CAPTION_MAX_BODY:-33554432}", "CAPTION_MAX_CONCURRENT": "${CAPTION_MAX_CONCURRENT:-4}"}, "networks": ["parse-caption", "public"]}
    return doc


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--client", action="append", required=True, help="a service of the client stack that posts jobs (repeatable)")
    ap.add_argument("--dev-store", help="host:port of a store inside the stack the proxy may reach")
    ap.add_argument("--dev-store-network")
    ap.add_argument("--ocr", action="store_true", help="add the ocr-sidecar (OCR_PROFILES must be set at deploy)")
    ap.add_argument("--asr", action="store_true", help="add the asr-sidecar (ASR_PROFILES must be set at deploy)")
    ap.add_argument("--caption", action="store_true", help="add the caption-sidecar (CAPTION_PROFILES must be set at deploy)")
    ap.add_argument("--parser-image", help="the client's own parser image (FROM the base image)")
    a = ap.parse_args(argv)
    print(json.dumps(overlay(a.client, a.dev_store, a.ocr, a.asr, a.caption, a.parser_image, dev_store_network=a.dev_store_network), indent=2))


if __name__ == "__main__":
    main()
