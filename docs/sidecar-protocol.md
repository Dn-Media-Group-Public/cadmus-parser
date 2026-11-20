# The sidecar protocol (and how to write a sidecar)

A sidecar is a service the sandboxed parser may use without holding what it needs: a remote OCR endpoint and its API key, a translation model, a virus scanner.
The parser holds one **token**; the sidecar holds everything else. Each sidecar stays specific to its job and does its own work behind this protocol. The
`ocr-sidecar` in this repository is the reference implementation.

## Wiring

- The sidecar keeps a **profile table**: opaque token -> everything about the profile (upstream URL, model, API keys, prompt ...). The token is an **opaque
  secret that indexes that table**; it carries no data. Compare tokens in constant time, without an early exit.
- The wrapper's `<FEATURE>_PROFILES` table (e.g. `OCR_PROFILES`) maps profile name -> `{url, token}` of the sidecar. A job that asks for `features: {"ocr": "lan"}`
  gets only that profile's `OCR_URL` and `OCR_TOKEN` in the parser's environment; the sidecar's own table is never in the wrapper.
- A new kind of sidecar is added by adding an entry to `FEATURES` in the wrapper (feature name -> environment prefix), not code, and a processor that calls it.

## The protocol

Plain HTTP, bytes in and bytes out.

- **`POST /process`**: one file in, parsed data out. The request body is the input bytes. Headers: `Authorization: Bearer <token>` (selects the profile),
  `Content-Type` (MIME type of the body), `Content-Disposition` (filename, optional); any other header is allowed and is used as that sidecar sees fit. Options
  travel as extra request headers that the sidecar interprets, named with the standard `X-` prefix only (`X-Language`). Nothing else names the feature or the
  profile: what the sidecar does and which profile it uses is controlled by the token and the file.
- **The response** body is the result bytes, with its own `Content-Type` and, where relevant, `Content-Disposition`, both set by the sidecar.
- **`GET /health`**: `200 {"ok": true}`, no authentication.
- No other methods or routes.

**A sidecar is not a proxy.** It forwards no request header, ever. It reads the headers it needs and builds what it sends upstream itself. It takes no URL and no
path from the request and has no store access.

## Errors and limits

Standard HTTP status codes with their standard meanings; the protocol defines none of its own (`401` bad token, `413` too big, `415` type not accepted, `502`
upstream failed, `503` busy ...). An error response has a JSON body with `error` (a string) and optionally `traceback` (a string). Whether a traceback is sent, and
what it contains, is the sidecar's own decision: it knows what could leak.

Each sidecar defines its own size cap, concurrency and timeouts; there are no protocol-wide numbers. It **must** enforce some request-size limit and answer `413`
when it is exceeded, answer `503` when it is busy, and serve `GET /health`.

## Egress

A sidecar that reaches an upstream should pin its own egress (iptables rules applied at start as root with `NET_ADMIN`, then dropped): see
`common/egress_rules.py` (`pinned_rules` for a fixed set of endpoints, `public_rules` for a proxy). Put it on an internal network shared only with the parser.

There is a written spec only: no conformance suite and no skeleton service. The library's own sidecars have unit tests (`ocr-sidecar/tests`).
