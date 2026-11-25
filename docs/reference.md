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
- `features` is `{name: profile}`; `ocr`, `asr`, `asr-fallback`, `caption` and `caption-fallback` ship (primary and fallback model of [ASR](#asr) and [caption](#caption)). Unknown name or profile: 400.
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

Written under the output base; `index.json` first, then `status.json`. Never listed inside the index. Example output trees per input kind: [output examples](output-examples.md).

```json
{"status": "running" | "done" | "failed", "owner_replica": "...", "updated": 1700000000.0, "error"?: "...", "traceback"?: "...", "usage"?: {"calls": [...], "truncated": false, "error"?: "..."}}
```

`usage` is written once the job ends (done or failed), only if the job asked for a feature whose sidecar reports usage (`admin_token` in its `<PREFIX>_PROFILES`
entry). One record per upstream attempt, written by the trusted sidecar, never the processor: `{"feature", "profile", "model", "outcome": "ok" | "error" (a sidecar
may also use "invalid" | "blocked"), "unit": "tokens" | "audio_seconds", "input", "output", "t"}`. At most `PARSE_USAGE_MAX_CALLS` records are kept (first ones),
then `truncated` is true. `error` means a sidecar could not be reached to register or collect: the job result is unaffected and the records of that feature are missing.
See also: [Plan: sidecar usage reporting](implemented-plans/03-sidecar-usage-reporting.md)

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

The overlay (`python -m cadmus_parser_server.overlay --client web --client worker [--dev-store host:port] [--ocr] [--asr] [--caption] [--parser-image my/parser:1]`) adds a
network `<client>-parse` per client service that only that client and the parser are on, `PARSE_URL=http://parser:8080` and
`PARSE_TOKEN=${WRAPPER_TOKEN}` in each client, your image for `parser`, the dev store's network for the proxy and the `ocr-sidecar` / `asr-sidecar` / `caption-sidecar` with `--ocr` / `--asr` / `--caption`.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `WRAPPER_TOKEN` | parser; clients as `PARSE_TOKEN` | the wrapper's bearer token (a secret) |
| `PROXY_TOKEN` | parser, blob-sidecar | the wrapper's credential to the egress proxy; different from `WRAPPER_TOKEN` |
| `PARSE_PROXY_HOST` | parser | `host:port` of the proxy (stack file: `blob-sidecar:3128`) |
| `PARSE_PROXY_ALLOW` | blob-sidecar | `host:port` exceptions to "public internet only" |
| `PARSE_BLOB_OPTIONS` | parser | JSON fsspec options merged under every credential (endpoint ...); **no secrets** |
| `OCR_PROFILES` | parser | JSON `{profile: {url, token}}`: the sidecar to use and its token; the parser gets only the requested profile's. (The ocr-sidecar's own `OCR_PROFILES` is a different table.) |
| `<PREFIX>_PROFILES[...].admin_token` | parser | optional, per profile entry: the sidecar's `<PREFIX>_ADMIN_TOKEN`; enables usage reporting for that sidecar (never reaches the parser) |
| `PARSE_USAGE_MAX_CALLS` | parser, sidecars | most usage records kept per job (default 20000) |
| `OCR_ADMIN_TOKEN` | ocr-sidecar | enables the admin routes for usage reporting |
| `ASR_PROFILES`, `ASR_FALLBACK_PROFILES` | parser (stack file: `PARSE_ASR_TABLE`, `PARSE_ASR_FALLBACK_TABLE`) | like `OCR_PROFILES`, for `features.asr` and `features.asr-fallback` (both may name profiles of the same sidecar) |
| `ASR_PROFILES`, `ASR_ADMIN_TOKEN`, `ASR_MAX_BODY`, `ASR_MAX_CONCURRENT`, `ASR_TIMEOUT` | asr-sidecar | its own profile table (see ASR below), usage reporting, caps |
| `CAPTION_PROFILES`, `CAPTION_FALLBACK_PROFILES` | parser (stack file: `PARSE_CAPTION_TABLE`, `PARSE_CAPTION_FALLBACK_TABLE`) | like `ASR_PROFILES`, for `features.caption` and `features.caption-fallback` |
| `CAPTION_PROFILES`, `CAPTION_ADMIN_TOKEN`, `CAPTION_MAX_BODY`, `CAPTION_MAX_CONCURRENT`, `CAPTION_TIMEOUT` | caption-sidecar | its own profile table (see Caption below), usage reporting, caps |
| `CAPTION_TIMEOUT`, `CAPTION_BUSY_WAIT` | parser (forwarded) | seconds per sidecar call (default 120); seconds to wait, times the try number, on a 503 (default 3, up to 5 tries) |
| `ASR_TIMEOUT`, `ASR_BUSY_WAIT` | parser (forwarded) | seconds per sidecar call (default 600); seconds to wait, times the try number, when the sidecar answers 503 (default 3, up to 5 tries) |
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
| `MAX_PARSE_BYTES_FILES`, `MAX_PARSE_BYTES_IMAGE`, `MAX_PARSE_BYTES_VIDEO`, `MAX_PARSE_BYTES_SOUND` (each falls back to `MAX_PARSE_BYTES`; see [per-processor size caps](implemented-plans/parse-bytes-per-processor.md)), `MAX_PARSE_BYTES`, `MAX_ARCHIVE_BYTES`, `METADATA_TIMEOUT`, `METADATA_MAX_MEM`, `METADATA_MAX_BYTES`, `OCR_TIMEOUT`, `OCR_LANGS`, `OCR_MIN_CONF`, `OCR_MIN_CHARS`, `OCR_DPI`, `OCR_MAX_PAGES`, `OCR_MAX_SIDE`, `OCR_TESSERACT_TIMEOUT` | parser (forwarded) | settings of the default `files` processor |
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

`media-decode` writes a frame store (a 2 h video: about 100 MB with the default `540@10`, 1 GB more for `full@0.5` at 1080p) and may need more than the `video` deadline for long videos: raise `output_mib` and `deadline_s` of `video`. Options and outputs: [media-decode](processors.md#media-decode); `media` is its `top_dirs` entry. See also: [Plan: media decode](implemented-plans/02-media-decode.md)

Built-ins for `video` (3600 s, 16 GiB RAM, 16 GiB file, 20 GiB in/out), `audio` and `archive` (1800 s, 8 GiB RAM, 4 GiB input, 8 GiB output) are larger. **Every
built-in is finite, for every class**; `0` is accepted only when the operator writes it. A class has its built-in, overridden by `default.*`, overridden by `<class>.*`.

**The wrapper picks the class.** After pulling the tree it classifies each file by extension and, failing that, by its first 16 bytes against a built-in table of
magic numbers (nothing is decoded). The job runs under the **per-limit maximum over the classes present** (0 counts as the largest): a photo next to a video
gets the video's limits. A client that needs tighter limits splits the tree into one job per class itself. An archive counts as `archive` for itself and
everything in it (it is only unpacked inside the parser, under the limits already chosen): set `archive.*` generously.

`input_mib` is checked twice: before the pull, against the largest of any class (the operator's ceiling), from the object listing (the credential needs list
permission on the input base); after the pull and classification, against the job's own class, before the parser starts. The disk bound is
`input_mib` + `output_mib` + `fsize_mib`. A caller can neither pass nor raise a limit.

## ASR

`features: {"asr": "<profile>", "asr-fallback": "<profile>"}` (the fallback is optional) turns on transcription of audio and of video with sound. Needs `media-decode`
(its `audio.opus`) and `ffmpeg`. Options, `options["asr"]`: `chunk_seconds` (300; 0 = the whole file), `chunk_overlap_seconds` (30), `language` (unset: the model detects it),
`context` (names, terminology, topics; for a `transcriptions` model write it as transcript-style text), `workers` (4, capped by `PARSE_MEMBER_WORKERS`).

Output, `asr/<file.rel>/transcript.json` (the processors' own record: advisory; trusted cost data is the sidecar's usage record in `status.json`):

```json
{"language": "...", "speaker_scope": "full|partial", "duration": 3612.4,
 "chunks": [{"start": 0, "end": 330, "outcome": "ok|degraded|blocked|failed", "role": "primary|fallback|none", "model": "...", "attempts": 1, "reason": "...", "notes": []}],
 "segments": [{"offset": 12.4, "speaker": "Person 1", "text": "...", "chunk": 0}]}
```

`outcome` `degraded` = every model attempt for the chunk failed validation and the one with the most segments is kept; `blocked` = every attempt was blocked by the model's safety
filter; `failed` = no usable text and not (only) blocked. The job's `asr-split` raises (a note on the input's entry) only if no chunk has text. `speaker_scope` is `partial`
when two chunks' speaker labels could not be matched (no matchable speech in their overlap), so the same person may appear under several labels.

The `asr-sidecar` profile table (`ASR_PROFILES`): `{name: {token, base_url, model, backend: "transcriptions" (default) | "chat", auth: none|bearer|google|oauth2, api_key?, audience?, prompt?,
max_output_tokens? (chat, default 32768), audio_format? (chat, default "ogg"), response_format? (transcriptions, default "verbose_json"; "diarized_json" for speakers), form? (extra form fields)}}`.
It makes one upstream call and does no retry, validation or fallback. `chat` exists for languages without a diarization model (the audio goes to an audio-capable LLM, the answer is
JSON-schema constrained segments `{start: "MM:SS", speaker, text}`).

Known limitations: a compressed timeline goes undetected; retrying a blocked chunk on the same model never helps (only the fallback does, so the retry is skipped); a `transcriptions` backend
without diarization yields no speaker labels; speaker mapping across chunks depends on speech in the overlap.
See also: [Plan: ASR](implemented-plans/04-asr.md)

## Frame OCR

`features: {"ocr": "<profile>"}` also turns on `frame-ocr`: the OCR engine of images and scanned PDFs (Tesseract, the OCR sidecar when the result is poor; same `OCR_*` settings) run over one
frame pass of a video's frame store ([media-decode](implemented-plans/02-media-decode.md)). Options, `options["frame-ocr"]`: `pass` (the stem of a `frame_passes` file such as `full-0.5`; default
the highest-resolution pass with fps <= 1, else the highest-resolution one), `change_threshold` (0.02; a frame whose downscaled grey image differs from the last OCRed one by less than this
mean fraction is not OCRed, it extends the current entry), `max_frames` (5000 OCR runs; a note on the input's entry says when it was reached, the output holds what was read before).

Output, `frame-ocr/<file.rel>/ocr.json`: `[{"t_start": 12.0, "t_end": 18.0, "text": "...", "conf": 71, "engine": "tesseract|remote"}]`; consecutive near-identical results (ratio >= 0.9) are one
entry, an empty result ends it, `conf` is null for `remote`. Nothing is written for images, audio, or a video without a frame pass.
See also: [Plan: Frame OCR](implemented-plans/06-frame-ocr.md)

## Caption

`features: {"caption": "<profile>", "caption-fallback": "<profile>"}` (the fallback is optional) turns on captions of images and of video keyframes by a vision model. Needs `media-decode`
(its keyframes, `web.png`) and, for scaling, `Pillow`. Options, `options["caption"]`: `max_side` (1536; frames are scaled down to this before sending), `window_frames` (20, at most 20;
consecutive keyframes captioned together), `workers` (4, capped by `PARSE_MEMBER_WORKERS`). An image is one unit, a video is one unit per window of keyframes.

Output, `caption/<file.rel>/captions.json` (the processors' own record: advisory; trusted cost data is the sidecar's usage record in `status.json`):

```json
{"kind": "image|video",
 "captions": [{"t_start": 0.0, "t_end": 94.2, "text": "...", "outcome": "ok|blocked|invalid", "role": "primary|fallback|none", "model": "...", "frames": [0, 1, 2]}]}
```

An image has one entry with `t_start = t_end = 0`; a window ends where the next begins (the last at the video's duration). Each unit is tried on the primary (twice on `invalid`, once on
`blocked`), then the fallback; a unit nobody captioned keeps `outcome` `blocked` (every attempt) or `invalid`, empty `text`, and is noted on the input's entry; it never fails the job.
The caption may use Markdown emphasis; the default prompt forbids raw HTML.

**Upstream auth of the model sidecars** (`ocr`, `asr`, `caption`; `auth` in each profile): `none`, `bearer` (`api_key`), `google` (ID token for `audience`) or `oauth2`, the OAuth2
client-credentials grant: `token_url`, `client_id`, `client_secret` (or `client_secret_env`: the name of an environment variable holding it, which wins), `scope?`, `audience?`
(sent as the `audience` form field), `token_auth?` (`post` default: credentials in the body; `basic`: HTTP Basic). The sidecar caches the token in memory per
(`token_url`, `client_id`, `scope`, `audience`), refreshes it when fewer than max(30 s, 10 %) of its lifetime remain (no `expires_in`: 60 s), and on a 401/403 from the model drops
it and retries once. A failing token endpoint answers `502 {"error": "oauth2 token: <status>"}` and writes no usage row. Missing `token_url`/`client_id`/secret fails the sidecar at start.
The pinned egress of the sidecar also allows the `token_url` host. See also: [Plan: OAuth2 upstream auth](implemented-plans/oauth2-upstream-auth.md)

The `caption-sidecar` profile table (`CAPTION_PROFILES`): `{name: {token, base_url, model, auth: none|bearer|google|oauth2, api_key?, audience?, prompt?, max_output_tokens? (default 2048)}}`
(OpenAI-compatible chat completions, frames as `image_url` data URIs). `POST /process`: an image body (`image/*`), or `multipart/mixed` of up to 20 `image/*` parts with `X-Frame-Times`
(seconds, comma separated, one per part); answer `{text, finish_reason, block_reason, model}`. One upstream call, no retry, validation or fallback.
See also: [Plan: Caption](implemented-plans/05-caption.md)

## media-markdown

For an image, video or audio file decoded by `media-decode`, `media-markdown` writes `parsed/<rel>.md` (no `terse/`: it would hold nothing useful) from whichever of `asr`, `caption` and `frame-ocr` produced a result: one paragraph per item, in time order, no headings. A speech segment is `` `0:06` **B:** text `` (no name when the ASR gave none), a caption `` `0:08` _SCENE:_ text ``, a frame-OCR entry `` `0:05` _ON SCREEN:_ text `` (multi-line OCR text joined with ` / `). An image has no times: its caption, then its OCR text. Captions that were skipped or blocked leave no line. Nothing is written when no part exists; the file is still claimed, so `documents` never sees media. The three JSON outputs are unchanged.
See also: [Plan: media-markdown](implemented-plans/media-markdown.md)
