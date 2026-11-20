"""The stack file overlay for one consumer stack: `python -m cadmus_parser_server.overlay --consumer web --consumer worker [--dev-store seaweedfs:8333]
[--ocr] [--parser-image myorg/my-parser:1] > parse-overlay.yml`, deployed after docker-compose.parse.yml (`docker stack deploy -c … -c …`).

It adds what depends on the consumer: a network `<consumer>-parse` per consumer service that only that consumer and the parser are on (one shared
network would let the consumers reach each other), PARSE_URL and PARSE_TOKEN (= ${WRAPPER_TOKEN}) in each consumer, the parser on all of those
networks, the consumer's own image for `parser`, the dev store's network for the egress proxy, and the `ocr-sidecar` with OCR. The output is JSON,
which is YAML; a consumer's own per-service additions go in `extra_env`."""

import argparse
import json


def overlay(consumers: list[str], dev_store: str | None = None, ocr: bool = False, parser_image: str | None = None, extra_env: dict | None = None,
            dev_store_network: str | None = None) -> dict:
    """`dev_store` = host:port of a store inside the stack that the proxy may reach (exactly that address and port); its network is `dev_store_network`
    (default: the host name). `extra_env` = {service: {VAR: value}} merged into the consumers' environment."""
    nets = {c: f"{c}-parse" for c in consumers}
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
    return doc


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--consumer", action="append", required=True, help="a service of the consumer stack that posts jobs (repeatable)")
    ap.add_argument("--dev-store", help="host:port of a store inside the stack the proxy may reach")
    ap.add_argument("--dev-store-network")
    ap.add_argument("--ocr", action="store_true", help="add the ocr-sidecar (OCR_PROFILES must be set at deploy)")
    ap.add_argument("--parser-image", help="the consumer's own parser image (FROM the base image)")
    a = ap.parse_args(argv)
    print(json.dumps(overlay(a.consumer, a.dev_store, a.ocr, a.parser_image, dev_store_network=a.dev_store_network), indent=2))


if __name__ == "__main__":
    main()
