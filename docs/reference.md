# Reference

## Wrapper HTTP API

Every request carries `Authorization: Bearer <WRAPPER_TOKEN>`. Every non-2xx answer has a JSON body `{"error": "<string>", "traceback"?: "<string>"}`
(a `traceback` only when the operator set `PARSE_TRACEBACKS=1`). There is **no GET of a job**: read `status.json` and `index.json` from the output base.

### `POST /jobs`

```json
{"input":  {"url": "s3://bucket/some/prefix", "options": {"key": "...", "secret": "...", "token": "..."}},
 "output": {"url": "s3://bucket/other/prefix", "options": {"...": "..."}},
 "options":  {"<processor name>": {"...": "..."}},
 "features": {"<feature>": "<profile>"}}
```

- `input.url` / `output.url` are fsspec URLs, the two **base** URLs and all the wrapper knows about layout. The wrapper pulls everything under the input base
  (a prefix, or a single object, which is a one-file tree) into the parser's input directory, relative paths preserved, and uploads what the parser writes
  under the output base. It never interprets the URLs beyond that. Anything fsspec accepts goes through as given: see the [security guide](security.md).
- `input.options` / `output.options` are the credential for that side (an fsspec `storage_options` fragment), merged over `PARSE_BLOB_OPTIONS`, in memory only.
  The two sides may be different stores. A GCS downscoped token travels as `gcs_access_token` (a string; `cadmus_parser.wire` converts).
- `options` is **keyed by processor name**; a processor sees only its own entry. There is no `processor` field: every installed processor that accepts a file runs.
- `features` is `{name: profile}`; only `ocr` ships. Unknown name or profile: 400.
- No class or limit fields: the wrapper picks the media class itself and only the operator sets numbers.

Answers: `202 {"status": "running", "id": "<opaque job id>", "owner": "<ip:port>"}`; `400` bad body, unknown feature or profile, credential rejected or output base
unusable; `401`; `409` a job with the same **output base URL** is running (on this replica, or its `status.json` shows `running` with a fresh heartbeat);
`503` no free parser (every replica has its own uid pool). `owner` is the address of the replica that took the job as seen on the caller's network (the local
address of the connection the POST arrived on). Cache `(id, owner)` for renewals. `status.json` says `running` before the 202 is sent.

### `POST /jobs/renew`

`{"id": "<job id>", "options": {"<url>": {...storage_options...}}}`, sent to the `owner` address. Each key must equal the job's `input.url` or `output.url`
exactly as posted (otherwise 400); that side's credential is replaced, in memory. `200`; `404` unknown job (or the replica does not own it); `410` finished.
The wrapper opens its filesystem per file transfer, so every pull and every upload part uses the newest credential. A job that stops receiving renewals fails
when its current credential expires.

### `GET /version`, `GET /health`

`{"api": "1"}`; `{"ok": true}`.

## `status.json` and `index.json`

Written under the output base; `index.json` first, then `status.json`. Never listed inside the index.

```json
{"status": "running" | "done" | "failed", "owner_replica": "...", "updated": 1700000000.0, "error"?: "...", "traceback"?: "..."}
```

`running` is rewritten every 10 s; a `running` older than 30 s means the wrapper is lost (the client library applies the rule: `Client.status`).

```json
{"files": {"<input path>": {"size": 123, "content_type"?: "...", "meta"?: {}, "notes"?: "...",
                            "outputs": {"<output path>": {"size": 45, "content_type"?: "...", "meta"?: {}}},
                            "files": {"<member path>": {"...same shape, for archive members...": null}}}},
 "notes"?: "<wrapper-level remarks>"}
```

- One entry per pulled input (also when nothing came out of it); input keys are relative to `input.url`, output keys to `output.url`. No outputs without an input.
  A fresh file per job: it is not merged with an existing one.
- `size` is the wrapper's own `fstat` for inputs and outputs; the size of an archive **member** is processor-claimed (it exists only in the parser's scratch).
- `content_type`, `meta` (an object of at most 2000 bytes as JSON, passed through) and `notes` (one string per entry, several joined with newlines) are declared
  by processors: **untrusted** and size-capped.
- Archives nest to 8 levels and `MAX_ARCHIVE_MEMBERS` entries; past either, a wrapper-level note says so and the job does not fail.
- The reason a job failed is `error` in `status.json`, not in the index.

## The worker protocol

The parser writes one JSON object per line (at most 4096 bytes, one `write`) on a private dup of its stdout pipe; the wrapper validates every one.

| Record | Meaning |
|---|---|
| `{"in": ["a.zip", "docs/m.pdf"], "path": "parsed/x.md", "content_type"?, "meta"?}` | an output file of that input (one element per archive level); the file is under `<jobdir>/out/` |
| `{"in": [...], "info": {"content_type"?, "meta"?, "size"?}}` | about the input itself (`size` only counts for members) |
| `{"in": [...], "note": "..."}` / `{"note": "..."}` | a note on the entry / on the whole index |
| `{"error": "..."}` | the worker could not start (a processor cycle); the job fails with this reason |

An unknown input, a bad path (absolute, `..`, outside the declared top-level directories, not a regular file, a symlink), a record that is not JSON, too long,
or too many (`max_records`) is a protocol violation: the job fails and the parser is killed. An output path is accepted once per job.

## Processor entry points

Group `cadmus_parser.processors`; see [How to write a processor](processors.md).

## Stack file

`docker-compose.parse.yml` is a swarm stack file (deploy with `docker stack deploy -c docker-compose.parse.yml -c overlay.yml ...`). It defines:

- `parser`: the wrapper; capabilities `SETUID SETGID CHOWN KILL SETPCAP` only (the entrypoint drops the rest and sets `no_new_privs`); networks `parse-egress`, `parse-ocr`
  (both `internal`); `PARSE_REPLICAS` replicas, each with its own uid pool.
- `blob-sidecar`: squid with Basic auth on every request, behind iptables rules for its own uid (public internet only; `PARSE_PROXY_ALLOW` = space-separated
  `host:port` exceptions, for a store inside your stack); networks `parse-egress` and `public`.
- the `public` network (the way out of the proxy); the same definition may be in your own stack file.

The overlay (`python -m cadmus_parser_server.overlay --client web --client worker [--dev-store host:port] [--ocr] [--parser-image my/parser:1]`) adds a
network `<client>-parse` per client service that only that client and the parser are on, `PARSE_URL=http://parser:8080` and
`PARSE_TOKEN=${WRAPPER_TOKEN}` in each client, your image for `parser`, the dev store's network for the proxy and the `ocr-sidecar` with `--ocr`.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `WRAPPER_TOKEN` | parser; clients as `PARSE_TOKEN` | the wrapper's bearer token (a secret) |
| `PROXY_TOKEN` | parser, blob-sidecar | the wrapper's credential to the egress proxy; different from `WRAPPER_TOKEN` |
| `PARSE_PROXY_HOST` | parser | `host:port` of the proxy (stack file: `blob-sidecar:3128`) |
| `PARSE_PROXY_ALLOW` | blob-sidecar | `host:port` exceptions to "public internet only" |
| `PARSE_BLOB_OPTIONS` | parser | JSON fsspec options merged under every credential (endpoint ...); **no secrets** |
| `OCR_PROFILES` | parser | JSON `{profile: {url, token}}`: the sidecar to use and its token; the parser gets only the requested profile's. (The ocr-sidecar's own `OCR_PROFILES` is a different table.) |
| `PARSE_POOL_SIZE` | parser | parsers per replica (default 4); a full replica answers 503 |
| `PARSE_UID_BASE` | parser | first pool uid (default 20000) |
| `PARSE_SCRATCH` | parser | scratch area (default `/scratch`; an anonymous volume) |
| `PARSE_LIMITS` | parser | JSON limits per class, below |
| `PARSE_PASS_ENV` | parser | extra variable names the parser process may see (processor settings, never secrets) |
| `PARSE_TRACEBACKS` | parser | `1`: tracebacks in `status.json` and error bodies |
| `PARSE_PORT` | parser | listen port (default 8080) |
| `REPLICA_ID` | parser | name in `owner_replica` (stack file: the task slot) |
| `PARSE_MEMBER_WORKERS` | parser | threads of `ctx.recurse_many` (default 4) |
| `MAX_SYNTHETIC_MEMBERS` | parser | cap of synthetic members (chunks, windows) per job (default 10000); see [processor handoff](implemented-plans/01-processor-handoff.md) |
| `MAX_ARCHIVE_MEMBERS` | parser | cap of nested index entries per job (default 50000), also used by the `files` processor |
| `MAX_PARSE_BYTES`, `MAX_ARCHIVE_BYTES`, `METADATA_TIMEOUT`, `METADATA_MAX_MEM`, `METADATA_MAX_BYTES`, `OCR_TIMEOUT`, `OCR_LANGS`, `OCR_MIN_CONF`, `OCR_MIN_CHARS`, `OCR_DPI`, `OCR_MAX_PAGES`, `OCR_MAX_SIDE`, `OCR_TESSERACT_TIMEOUT` | parser (forwarded) | settings of the default `files` processor |
| `PARSE_REPLICAS`, `PARSE_MEMORY` | stack file | replicas (default 1) and memory limit per replica (default 8g) |
| `OCR_MAX_BODY`, `OCR_MAX_CONCURRENT`, `OCR_TIMEOUT` | ocr-sidecar | its caps (see [the protocol](sidecar-protocol.md)) |

## Limits: `PARSE_LIMITS`

JSON, nested by class and then limit name:

```json
{"default": {"deadline_s": 600, "ram_mib": 6144},
 "video":   {"deadline_s": 3600, "ram_mib": 16384, "fsize_mib": 16384, "input_mib": 20480, "output_mib": 20480},
 "image":   {"ram_mib": 400}}
```

Classes: `video`, `audio`, `image`, `document`, `archive`, `other` (plus `default`, which applies to all). Names (the unit is part of the name):

| Name | Meaning | Built-in default (document, image, other) |
|---|---|---|
| `deadline_s` | wall clock per job, seconds; then the process group is killed | 600 |
| `ram_mib` | address space per parser, MiB (`RLIMIT_AS`); 0 = none, rely on the container limit | 6144 |
| `fsize_mib` | largest single output file, MiB (`RLIMIT_FSIZE`) | 4096 |
| `input_mib` | largest input tree, MiB; 0 = unlimited | 50 |
| `output_mib` | total uploaded for a job, MiB; 0 = unlimited | 4096 |
| `nproc`, `nofile` | process (thread) and open-file counts | 1024, 1024 |

Built-ins for `video` (3600 s, 16 GiB RAM, 16 GiB file, 20 GiB in/out), `audio` and `archive` (1800 s, 8 GiB RAM, 4 GiB input, 8 GiB output) are larger. **Every
built-in is finite, for every class**; `0` is accepted only when the operator writes it. A class has its built-in, overridden by `default.*`, overridden by `<class>.*`.

**The wrapper picks the class.** After pulling the tree it classifies each file by extension and, failing that, by its first 16 bytes against a built-in table of
magic numbers (nothing is decoded). The job runs under the **per-limit maximum over the classes present** (0 counts as the largest): a photo next to a video
gets the video's limits. A client that needs tighter limits splits the tree into one job per class itself. An archive counts as `archive` for itself and
everything in it (it is only unpacked inside the parser, under the limits already chosen): set `archive.*` generously.

`input_mib` is checked twice: before the pull, against the largest of any class (the operator's ceiling), from the object listing (the credential needs list
permission on the input base); after the pull and classification, against the job's own class, before the parser starts. The disk bound is
`input_mib` + `output_mib` + `fsize_mib`. A caller can neither pass nor raise a limit.
