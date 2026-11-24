# The sidecar protocol (and how to write a sidecar)

A sidecar is a service the sandboxed parser may use without holding what it needs: a remote OCR endpoint and its API key, a translation model, a virus scanner.
The parser holds one **token**; the sidecar holds everything else. Each sidecar stays specific to its job and does its own work behind this protocol. The
`ocr-sidecar` in this repository is the reference implementation; the `asr-sidecar` shows a JSON answer and several profiles of one service, the `caption-sidecar` an image or multipart request.

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
- **The response** body (JSON for the `asr-sidecar`: the raw segments or text of one upstream call and its finish/block status) is the result bytes, with its own `Content-Type` and, where relevant, `Content-Disposition`, both set by the sidecar.
- **`GET /health`**: `200 {"ok": true}`, no authentication.
- No other methods or routes (except the optional admin routes below).

**A sidecar is not a proxy.** It forwards no request header, ever. It reads the headers it needs and builds what it sends upstream itself. It takes no URL and no
path from the request and has no store access.

## Usage reporting (optional)

A sidecar that spends money on behalf of a job can report it. With an `ADMIN_TOKEN` configured it adds admin routes, authenticated by `Authorization: Bearer
<ADMIN_TOKEN>` (known to the wrapper only, never the parser):

- `POST /admin/jobs` `{"job", "profile", "ttl"?}` (the wrapper sends `<job id>.<feature>`, one record per feature, so two features on one sidecar never share one) -> `{"token"}`: a fresh job token that selects the same profile as the profile token and is tied to the job.
  The wrapper gives the parser this token in `<FEATURE>_TOKEN`; the sidecar attributes each call carrying it to the job. A job id in a header is never trusted.
- `GET /admin/jobs/<id>/usage` -> `{"calls": [...], "truncated": bool}`, and drops the record and the token. `DELETE /admin/jobs/<id>` drops without reading.
- Job tokens expire after `ttl` seconds (the wrapper sends the job's deadline plus a margin).

Record format and `status.json`: see the [reference](reference.md#statusjson-and-indexjson). `common/usage.py` implements all of it; the `ocr-sidecar`, `asr-sidecar` and `caption-sidecar` use it.
See also: [Plan: sidecar usage reporting](implemented-plans/03-sidecar-usage-reporting.md)

## Errors and limits

Standard HTTP status codes with their standard meanings; the protocol defines none of its own (`401` bad token, `413` too big, `415` type not accepted, `502`
upstream failed, `503` busy ...). An error response has a JSON body with `error` (a string) and optionally `traceback` (a string). Whether a traceback is sent, and
what it contains, is the sidecar's own decision: it knows what could leak.

Each sidecar defines its own size cap, concurrency and timeouts; there are no protocol-wide numbers. It **must** enforce some request-size limit and answer `413`
when it is exceeded, answer `503` when it is busy, and serve `GET /health`.

## Upstream auth

A sidecar that calls a model endpoint authenticates with its profile's `auth`: `none`, `bearer`, `google` or `oauth2` (client credentials, token cached and refreshed by the sidecar);
one implementation, `common/upstream_auth.py`. Fields: [reference](reference.md). See also: [Plan: OAuth2 upstream auth](implemented-plans/oauth2-upstream-auth.md)

## Egress

A sidecar that reaches an upstream should pin its own egress (iptables rules applied at start as root with `NET_ADMIN`, then dropped): see
`common/egress_rules.py` (`pinned_rules` for a fixed set of endpoints (the model hosts and, for `oauth2` profiles, the token host), `public_rules` for a proxy). Put it on an internal network shared only with the parser.

There is a written spec only: no conformance suite and no skeleton service. The library's own sidecars have unit tests (`ocr-sidecar/tests`, `asr-sidecar/tests`, `caption-sidecar/tests`).
