# Plan: the parser service as a generic library (git submodule)

> **Status: draft.** Everything under "Design" is a proposal; the items under "Open questions" need a decision before the step that
> depends on them. It builds on the implemented [Plan: parser wrapper](../implemented-plans/parser-wrapper.md) and moves that code, it does not
> redesign its security model. [Plan: OCR hybrid](ocr-hybrid.md) is affected (see "OCR").

## Problem / Goal

Everything that interprets an untrusted file (documents, images, archives, audio, video) must run as a process that holds no credential and has no
route out. The parser service solves that for this project: a small trusted wrapper moves bytes between the blob store and a sandboxed parser on a
pool uid, behind an authenticated egress proxy, with an optional sidecar for remote OCR.

Nothing in that mechanism is specific to this project's file types, but three things are hard-coded to it:

- the job address: `Job.prefix` is `projects/<proj>/data/<session>/<job>` and `UUID_RE` checks three UUIDs (`wrapper.py`). The library must know
  nothing about how a consumer lays out its store: it is given one input base URL and one output base URL, and that is all;
- the feature set: `FEATURES = {"ocr"}`, and the per-file parse rule (`parse.py`, `extract.py`, `metadata.py`) is the only thing the parser can run;
- the limits: one global `PARSE_RLIMIT_AS`, `PARSE_JOB_TIMEOUT`, `MAX_PARSE_BYTES` for every job, which suits a web page and not a 2-hour video
  (10 min wall clock; `RLIMIT_FSIZE` 4 GiB is smaller than some single outputs of a video decode).

Goal: a **generic library in its own repository, included here as a git submodule**, that any system deployed as a Docker Swarm stack can use to
run untrusted-file processing safely: the wrapper, the sandbox and uid pool, the worker protocol, both sidecars, the client, and a stack file to
deploy them. This project becomes its first consumer, with unchanged behaviour. Each consuming stack runs **its own instance** of the services
(own store, own tokens); what is shared is code and images, not a running service.

Non-goals: new processing features (video, face detection and so on belong to a consumer's own processor), a different isolation model (a
`sessiond`-style capability-free helper, gVisor, one container per job), merging the two sidecars, replacing the consumer's own storage code.

## Design

### What the library is

A repository `cadmus-parser` (`git@github.com:Dn-Media-Group-Public/cadmus-parser.git`, MIT), submodule path `lib/cadmus-parser/`, versioned with tags. It contains:

| Part | From | Notes |
|---|---|---|
| `wrapper` (`wrapper.py`, uid `Pool`, status/heartbeat, upload) | `parser/spider_hoarder_parser/wrapper.py` | generalised as below |
| worker protocol (`worker.py`: protocol fd dup, `PipeSink`, record format) | `worker.py`, the `Sink` class of `parse.py` | unchanged protocol; the worker imports the **processor** named in the job |
| `blob-sidecar` (squid, token auth, egress rules) | `blob-sidecar/` | unchanged |
| `ocr-sidecar` (profile table, token per profile, forwarder) | `ocr-sidecar/` | unchanged |
| `egress_rules.py` | the two copies in the sidecars | one copy for both. The browser's own rules in this project's `browser-sdk` stay a separate copy (they may drift on purpose, per the parser-wrapper plan) |
| `client` (POST, read `status.json`/`index.json` from the output base with backoff and the heartbeat rule, one retry on 503/refused connection) | `sdk/.../gateway/parse_client.py` minus the inline-text logic | the inline-text and terse logic is the consumer's |
| base image | `parser/Dockerfile` | wrapper, Python, tini, util-linux, fsspec; **no** processor and no format tools |
| stack file and overlay generator | `docker-compose.parse.yml`, `scripts/spider_hoarder.py parse-overlay` | see "Deployment" |
| default processor `files` (optional extra) | `parse.py`, `extract.py`, `metadata.py`, `ocr.py` | decided (open question 2): moves, `terse/`/`simplified/` included, groups renamed `Cadmus-*` |

It does **not** contain: scoped credential minting (the consumer mints, the wrapper only receives one per request), authorisation of end users
(the consumer authorises; the wrapper authorises only the consumer by its token, as today), or anything that reads the result and turns it into
tool output.

### Contract between a consumer and the library

Four surfaces, all documented in the library and versioned together (`GET /version` on the wrapper returns the API version; breaking changes bump
the major version and the consumer pins a tag):

1. **Wrapper HTTP API.** `POST /jobs` with the wrapper token and a body
   `{input: {url, options?}, output: {url, options?}, processor?, options?, features?}`:
   - `input.url` / `output.url` are the two base URLs (fsspec URLs, e.g. `s3://bucket/some/prefix`). The wrapper pulls everything under the input
     base (a prefix, or a single object, which is a one-file tree) into the parser's input directory, preserving relative paths, and uploads what
     the parser writes under the output base. It never interprets the URLs beyond that: no segment checks, no templates, no layout.
   - `input.options` / `output.options` are the credential for that side (the consumer's scoped credential, as an fsspec `storage_options`
     fragment), merged over `PARSE_BLOB_OPTIONS`, in memory only. Only option names on a per-scheme allow-list are accepted (see "Security
     changes"). The two sides may be different stores.
   - `processor` (default `files`) and `options` (an object only the processor interprets); `features` stays `{name: profile}`. There is no
     class or limit field: the wrapper decides the media class itself (see "Limits").
   - Answers 202 / 409 / 503 / 400 as today. The job's identity (for the 409 check and the in-memory state) is its output base URL.
   - **There is no GET.** The wrapper writes `status.json` (`running`, then `done`/`failed`, with `owner_replica` and `updated`) and `index.json`
     under the output base, `index.json` before `status.json`, and writes `running` before it answers 202. The consumer reads them from the store
     with the credential it already has; the client library implements the heartbeat rule (10 s rewrite, `running` older than 30 s is `failed`,
     "wrapper lost"). This removes the one place the wrapper needed to know a job's address on a read.
2. **Processor entry point.** A Python package registers `cadmus_parser.processors` entry points:
   `parse_job(sink, indir, job, work)`, exactly today's `parse.parse_job` signature, with no storage access and no network. The worker looks the
   name up in the installed entry points, so an unknown processor is a 400 at POST time and nothing the caller sends is imported.
3. **Stack file.** `docker-compose.parse.yml` (a swarm stack file, deployed with `docker stack deploy -c … -c …`) defines `parser`, `blob-sidecar`
   and, when profiles are configured, `ocr-sidecar`, with the networks and capabilities of today. The consumer includes it with `-c`.
4. **Environment variables**, listed in one table in the library docs (limits, tokens, `PARSE_BLOB_OPTIONS`, profiles, pool size).

### Generalisations (the only behaviour changes)

- **Input and output base URLs instead of a job address.** The `UUID_RE` check, `Job.prefix` and the `projects/…/data/…` layout leave the
  library. This project sends `input.url` = `…/<job>/original` and `output.url` = `…/<job>` (so its `original/`, `parsed/`, `metadata/` layout is
  just what its processor writes); a consumer that stores by content hash sends whatever prefixes it wants.
  What the wrapper still enforces on the output side, independent of layout: every path a parser announces is relative, has no `..`, is a regular
  file opened with `O_NOFOLLOW`, starts with a top-level name the processor declared, and so stays under the output base; size from its own
  `fstat`; record, line and total-byte caps; `index.json` and `status.json` reserved.
  This loosens the wrapper's confinement; see "Security changes" below for exactly what, and what replaces it.
- **Features registry.** `FEATURES` becomes a table in wrapper config: a feature name maps to a profile table (`<FEATURE>_PROFILES`:
  name → `{url, token}`); the wrapper still puts **only the requested profile's** `<FEATURE>_URL`/`<FEATURE>_TOKEN` in the parser's environment.
  Only `ocr` ships; a new kind of sidecar is added by adding an entry, not code.
- **Limits, per media class.** The operator configures limits in one JSON env var, `PARSE_LIMITS`, nested by class and then limit name; in dotted
  notation, as the docs and `.env.example` write it:

  ```
  default.deadline_s = 600    default.ram_mib = 6144    default.fsize_mib = 4096    default.input_mib = 50      default.output_mib = 4096
  video.deadline_s   = 3600   video.ram_mib   = 16384   video.fsize_mib   = 16384   video.input_mib   = 20480   video.output_mib   = 20480
  image.ram_mib      = 44
  ```

  - Names (the unit is part of the name): `deadline_s` (wall clock, seconds, then the process group is killed), `ram_mib` (address space per parser, MiB; 0 = no `RLIMIT_AS`, rely on
    the container memory limit), `fsize_mib` (largest single output file, MiB, `RLIMIT_FSIZE`), `input_mib` (largest input tree, MiB; 0 = unlimited;
    replaces `MAX_PARSE_BYTES`), `output_mib` (total uploaded for a job, MiB; 0 = unlimited), and `nproc` / `nofile` (plain counts, no unit) as today.
  - **Every built-in default is finite, for every class, including `video`** (open question 4): large where a class needs it, but never
    unlimited, so a job cannot exhaust the wrapper's scratch disk or memory. `0` (unlimited) is accepted only when the operator sets it
    explicitly in `PARSE_LIMITS`; the library never ships it. The disk bound is `input_mib` + `output_mib` + `fsize_mib`, since `RLIMIT_AS` does not limit disk.
  - A class has the built-in default of each limit (today's values), overridden by `default.*` if set, overridden by `<class>.*` if set. A limit
    not mentioned for a class falls through.
  - **The wrapper picks the class; neither the consumer nor the parser does.** The classes are a small fixed set defined by the library
    (`video`, `audio`, `image`, `document`, `archive`, `other`), so the operator's `PARSE_LIMITS` keys are known. After the wrapper has pulled the
    input tree, it classifies **each file** by extension and, failing that, by comparing its first 16 bytes with a built-in table of magic numbers
    (stdlib byte comparison; the wrapper decodes nothing). The job's limit set is, **per limit, the maximum over the classes present** (a tree
    with a photo and a video gets the video's deadline and the larger of the two `ram_mib` values; a limit of 0 = unlimited counts as the largest).
    An archive gets the `archive` class for itself and everything in it: its members are only known after unpacking, which happens inside the
    parser under the limits already chosen, so the operator should set `archive.*` generously and the existing archive caps
    (`MAX_ARCHIVE_BYTES`, `MAX_ARCHIVE_MEMBERS`) remain the bound on what an archive can expand to.
  - Why the wrapper and not the parser: `RLIMIT_AS`, `RLIMIT_FSIZE` and the deadline are fixed when the parser process is started and cannot be raised
    by it afterwards, so the class has to be known before the start; a parser-side classification would need a separate sniffing process first
    (a second start per job) and would let a hostile file name the class it wants. The wrapper reading 16 bytes per file as a byte comparison is
    the smallest thing that works. Ordering: the pull itself is capped at the largest `input_mib` of any class (the operator's ceiling), then the
    class's own `input_mib` is checked before the parser starts and the job fails with a clear status if it is exceeded.
  - **Only the operator sets numbers.** A caller cannot pass or raise a limit.
  - Hours-long jobs: the wrapper's heartbeat is unchanged; the client's poll window is a parameter (default 12 min), so a consumer with long
    classes polls longer or reads `status.json` asynchronously.
- **Output directories.** `TOP_DIRS` (the top-level names a parser may write) is declared by the processor, not hard-coded; `index.json` and
  `status.json` stay reserved.

### Security changes

**What the old design guaranteed.** Three layers kept a job inside its own data:

1. *Who may call:* the wrapper token. Only the gateways, on their per-backend parse networks, can reach the wrapper at all.
2. *Where it may touch:* the wrapper built every key itself. The store (`RESEARCH_BLOB_URL`: scheme and bucket) came from the operator's
   configuration; the only caller-supplied parts were three UUIDs, which the wrapper checked against a regex. So even a fully compromised caller
   could only make the wrapper read and write under `<root>/projects/<uuid>/data/<uuid>/<uuid>/`, on the operator's store.
3. *What it may touch:* the scoped credential the gateway mints (read the project's `data/`, write only `<session>/<job>/`, 900 s), enforced by
   the store.

The hostile process is the parser, and it is out of all three: it holds no credential, token or URL.

**What changes.** Layer 2 is gone. The caller now supplies two complete URLs (scheme, bucket, prefix) and the credential options. The wrapper
no longer knows what a "job location" looks like, so it cannot tell a job's prefix from any other prefix. Layers 1 and 3 and the parser's
confinement are unchanged.

**What this does and does not make possible**, by who the attacker is:

- *The hostile file or the parser (the main adversary).* Unchanged. URLs and credentials never reach the parser; the paths it announces are still
  validated (relative, no `..`, regular file via `O_NOFOLLOW`, declared top-level directory), so it can write only under the output base the wrapper
  chose to upload to, never elsewhere. File contents cannot influence the URLs, which the consumer takes from its own state.
- *A bug in the consumer that builds the wrong URL* (for example a path assembled from user text, with `..` or a different project's id).
  Before: layer 2 confined the wrapper to a UUID-shaped job path, layer 3 confined it to the credential's scope. Now only layer 3 confines it, and
  the wrapper will faithfully read or overwrite anything the credential permits. For this project the read credential is project-wide
  (`projects/<proj>/data/*`), so such a bug could now make the wrapper copy another session's files into a job's input or overwrite another job's
  `original/`, `parsed/`, `index.json` or `status.json` within that project (previously: only the one job path). The consumer already has direct
  access to the same data, so this is lost defence in depth, not lost secrecy from the consumer; the fix is a credential scoped to the job
  (which this plan documents as the consumer's responsibility) and the consumer building URLs from validated ids.
- *Someone who steals the wrapper token* (it is one shared secret, in the gateways' environment). Before: they also needed a credential the store
  accepts, and could only name job-shaped paths on the operator's store. Now, **without further limits**, they could name any bucket on any host
  reachable through the blob-sidecar, which has no destination allowlist and allows the whole public internet (it blocks private ranges, the
  metadata server and other stack services; the dev store is an explicit exception). The wrapper would become an authenticated fetcher and
  uploader: pull files from an attacker's bucket, run them through the parsers (free compute, and a way to attack the parsers from outside the
  intended path) and upload the results to another attacker-chosen bucket. It would not reach the real store without a credential for it.
- *fsspec options and URL chaining.* This one already exists today: the wrapper does `fsspec.core.url_to_fs(blob_url, **{**blob_options,
  **creds})`, so the gateway-supplied credential dict goes into fsspec as keyword arguments, unfiltered. For `s3fs` that includes `endpoint_url`
  and `client_kwargs`; for `gcsfs` `token` can be a path to a local credentials file. With a caller-chosen URL it gets worse: fsspec URLs can be
  chained (`simplecache::s3://…`), can name other filesystems (`file://`, `http://`, `sftp://`, `zip://`), and the filesystem is a plug-in
  chosen by the scheme. Unfiltered, a caller could make the wrapper read local files of its container (its own 0600 secret files are in reach of the
  wrapper uid) or make HTTP requests. This is the most dangerous part of the loosening and also a hole to close now, independent of this plan.
- *Overlapping jobs.* The job's identity is its output URL. Two jobs whose output bases are equal or nested (`x/` and `x/y/`) share or contain each
  other's `status.json`, `index.json` and artefacts: one can report the other as `running`/`done`, or be restarted by it as "stale". The old
  design had the same collision only for an identical job triple; nesting could not occur.
- *Resource abuse.* A caller can name an arbitrarily large input tree. The `input_mib` limit is checked against the object listing before anything is
  pulled (it needs a listing right in the credential, which any working job has).

**What replaces layer 2.** None of these is as strong as "the wrapper builds the key", but together they restore a confinement the operator
controls instead of one fixed in the code:

1. **`PARSE_ALLOWED_BASES` is required.** A list of URL prefixes (scheme, bucket, path prefix) the wrapper will accept for `input.url` and
   `output.url`; the wrapper refuses to start without it. The consumer's old value, the operator's `RESEARCH_BLOB_URL`, is the natural default
   for a single store. This restores "only the operator's store", at store-root granularity instead of job granularity. Checked on the
   normalised URL (no `..` components, no encoded slashes, no trailing-slash tricks) and before anything is pulled.
2. **Scheme allow-list.** Only the schemes the operator enables (`s3`, `gs`/`gcs`); a URL containing `::` (chaining) or any other scheme is a 400.
3. **Option allow-list per scheme.** Only named options pass into fsspec (credentials: `key`, `secret`, `token`/`gcs_access_token`, `session`
   token; `endpoint_url` only if it equals the operator's configured endpoint). Anything else is a 400. `token` given as a string path is refused
   for `gcsfs` (only a downscoped access token string is accepted, as today's `gcs_access_token` path does).
4. **No overlapping jobs.** The wrapper refuses (409) a job whose output base equals, contains or is contained by the output base of a running
   job on that replica, and writes `status.json` with an ownership check across replicas by reading the existing one first (as the stale-heartbeat
   rule already does).
5. **Job-scoped credentials stay the consumer's job** and are stated as a requirement in the library docs: write limited to the output base, read
   limited to the input base. The wrapper cannot verify it. A test in the library's own suite shows the failure mode with an over-broad credential.
6. **Input size checked before pull** against the pre-classification ceiling (see Limits).

**Residual risk to accept or reject** (listed in open question 10): confinement is now the operator's allow-list plus the credential, not
the job. A stolen wrapper token plus a stolen or over-broad credential reaches whatever that credential reaches within the allowed roots, which
is the same as the consumer could do directly; what is lost is only the wrapper's own extra layer.

### Images and process layout

The library builds a **base image** (`<IMAGE_PREFIX>cadmus-parser-base:<tag>`). A consumer's own Dockerfile starts `FROM` it, installs its processor
package and its format tools (ExifTool, ffmpeg, Tesseract, `unar`, …), and the entry script of the base image starts the wrapper. One consumer image
per stack; the sidecars are used as built by the library. Build context concerns (this project builds with the repo root as context) are handled by
`deploy.sh` building the base image from the submodule directory first.

### OCR

Tesseract runs on untrusted pixels, so it is part of a processor inside the sandbox; the remote step goes through the `ocr-sidecar` because the
sandboxed process cannot hold endpoint credentials. Both therefore live in the library with the `files` processor (`ocr.py`), and
[Plan: OCR hybrid](ocr-hybrid.md) is implemented **there**: this plan only moves the sidecar client and the wrapper's profile selection; the
Tesseract pass, the confidence gate and the PDF rasterising are that plan's work, done in the library repo. That plan gets an amendment pointing to
this one when this plan is implemented.

### Deployment

- Submodule at `lib/cadmus-parser/`, pinned to a tag. `git clone --recurse-submodules`; `deploy.sh` runs `git submodule update --init` and fails with a
  clear message if the directory is empty. Docs and `README.md` say so.
- `deploy.sh` includes `lib/cadmus-parser/docker-compose.parse.yml` where it includes `docker-compose.parse.yml` today; the project-specific overlay
  (a `<backend>-parse` network per gateway, `PARSE_URL`/`PARSE_TOKEN` in each gateway, the dev store's network, `PARSE_PROXY_ALLOW`) stays here
  but is produced by the library's small overlay generator, which takes a list of consumer service names and an optional dev-store address and prints a
  stack file fragment. The generator is the library's only deploy-time code; this project's `scripts/spider_hoarder.py` calls it.
- Only Docker Swarm stacks are supported and documented (no other orchestrator).

### Migration (this project)

Behaviour must be identical after each step, with the existing parser tests passing against the moved code.

1. Create the library repo by **cloning this repository** (full history), pointing the clone's `origin` at
   `git@github.com:Dn-Media-Group-Public/cadmus-parser.git`, and turning the clone into the library: remove everything except `parser/`,
   `blob-sidecar/`, `ocr-sidecar/`, `docker-compose.parse.yml` and `LICENSE`, rename the package to `cadmus_parser` and the images to
   `cadmus-parser*`, and add the library's own README and CI (the existing unit tests). Nothing is pushed until the owner says so.
2. Replace the three directories here with the submodule; fix build contexts in `deploy.sh` and the stack file.
3. Generalise in the library, one commit per item, each with a test: input/output base URLs (with the URL allow-lists) and removal of the GET status endpoint, per-side credential pass-through, features
   registry, per-class limits (`PARSE_LIMITS`), processor entry points, output directories. This project sends `input` = `<job>/original`,
   `output` = `<job>`; its config reproduces the old limits as `default.*`.
4. Move the `files` processor behind an entry point; this project selects it by default. Rename the `SpiderHoarder-*` metadata groups to `Cadmus-*`
   in the library and every place here that reads them, in one step.
5. Move the client; `gateway/parse_client.py` shrinks to the inline-text and terse logic on top of it.
6. Overlay generator; `parse-overlay` here delegates to it.
7. Docs (below) and `docs/implemented-plans/index.md`; move this plan.

### Library documentation (requirement)

The library repo must be usable by a project that has never heard of spider-hoarder, so its docs contain no spider-hoarder concepts or examples:

- `README.md`: the problem, the threat model, what is and is not protected, the parts.
- Quickstart: a minimal consumer swarm stack and a toy processor, end to end.
- Reference for the four contract surfaces (wrapper HTTP API, processor entry point, stack file, environment variables), with the environment
  variable table and the limits (`PARSE_LIMITS`) reference.
- "How to write a processor" and "how to build a consumer image".
- Security guide: what the consumer must do (job-scoped credentials, building URLs from validated ids, keeping the wrapper token away from
  untrusted data), and `PARSE_ALLOWED_BASES`.
- Versioning and upgrade notes (API version, tags).

## Open questions

1. **Name, repo location and licence.** **Decided:** name `cadmus-parser`, repository
   `git@github.com:Dn-Media-Group-Public/cadmus-parser.git`, licence MIT, the same as spider-hoarder. The library must not carry this project's
   name and must be fully usable, and documented, for a separate project that knows nothing about spider-hoarder (see "Library documentation").
2. **The default `files` processor.** **Decided:** it moves into the library (markitdown, trafilatura, archives, binary decoders,
   ExifTool/`ffprobe`/GDAL metadata, OCR), as an optional extra. The `terse/` and `simplified/` outputs move with it and are always written;
   consumers that do not want them ignore them (no `outputs` option). The ExifTool extension groups are renamed from `SpiderHoarder-<Format>` to
   `Cadmus-<Format>` (`Cadmus-Media`, `Cadmus-Raster`, `Cadmus-Vector`, `Cadmus-Office`, `Cadmus-PDF`); this changes output this project reads
   today, so the rename and the reading code here are changed in the same step (migration step 4).
3. **History.** **Decided:** keep it by cloning this repository in full and reducing the clone to the library (migration step 1), instead of
   `git filter-repo` or a squashed first commit. The new repo therefore carries the whole of this project's history, including paths that are
   not part of the library.
4. **Heavy decoders and `ram`.** **Decided:** large but finite built-in defaults for every limit of every class, so the wrapper cannot run
   out of disk or memory; `0` (unlimited) stays possible only as an explicit operator setting. The numbers for heavy classes are chosen when the
   first such processor is added; the example in "Limits" is illustrative.
5. **Units and names of the limits.** **Decided:** self-describing names with the unit as a suffix: `deadline_s` (seconds), `ram_mib`,
   `fsize_mib`, `input_mib`, `output_mib` (MiB), plus the plain counts `nproc` and `nofile`. All the limit names above, in the examples and in the
   library docs and `.env.example`, use these.
6. **Classification by the wrapper: the details.** The class set (`video`, `audio`, `image`, `document`, `archive`, `other`), the extension and
   magic tables, and "maximum per limit over the files present" are proposals. The alternative the user named, classification inside the
   sandbox, is described in the Limits section and not recommended. A tree that mixes a tiny file and a huge video gets the video's limits for
   the whole job; per-file jobs would avoid that but need the consumer to split the tree.
7. **No GET status endpoint.** Recommended (the consumer reads `status.json` from the store it already has credentials for, which is what lets
   the wrapper stay ignorant of layout); the alternative keeps a `POST /jobs/status` taking the output URL and credential.
8. **One shared running service across stacks?** The design says no (each stack runs its own, because the store and secrets are per stack). If a
   future consumer wants a shared one, it needs per-consumer wrapper tokens and per-consumer `PARSE_ALLOWED_BASES`; not designed here.
9. **Sidecar kinds beyond OCR.** The features registry allows more; the `ocr-sidecar` request format (`POST /ocr`, image in, text out) is not
   generalised until a second kind exists.
10. **Is operator allow-listing enough in place of key construction?** The plan says yes (see "Security changes"), at store-root granularity. If a
    consumer needs finer confinement inside the wrapper, the library could take an optional `PARSE_JOB_REF` regex that the *consumer's* URLs must match
    (a pattern, not a template: the library still would not build keys); not planned.

## Files to change

| File | Change |
|---|---|
| `lib/cadmus-parser/` (new submodule), `.gitmodules` | the library |
| `parser/`, `blob-sidecar/`, `ocr-sidecar/` | removed (moved into the library) |
| `parser/Dockerfile` replacement (here) | `FROM` the library base image; installs the processor, ExifTool, ffmpeg, `unar` |
| `docker-compose.parse.yml` | removed here; the library's file is included |
| `deploy.sh` | submodule init and check, base image build, stack file path |
| `scripts/spider_hoarder.py` | `parse-overlay`, `parse-env`, `build-parse` delegate to the library generator |
| `sdk/spider_hoarder_sdk/gateway/parse_client.py` | thin layer over the library client |
| `.env.example` | `PARSE_LIMITS` (dotted notation documented), `PARSE_ALLOWED_BASES`, processor settings, with this project's values |
| `README.md`, `CLAUDE.md` | clone with `--recurse-submodules`; where the parser code now lives |

## Docs to update

- `docs/architecture/jobs-and-storage.md` (the parser service section) and `docs/architecture/overview.md` (service table, file table): the code
  now lives in the library; keep the security description and link to the library docs.
- `docs/architecture/backend-plugins.md`: parse wiring through the generator.
- `docs/operations/deploy-and-configure.md`: submodule, the new variables, per-class limits (`PARSE_LIMITS`).
- `docs/implemented-plans/parser-wrapper.md`: a "See also" to this plan; `docs/plans/ocr-hybrid.md`: amended to say the work is in the library.
- `docs/plans/index.md` (now), `docs/implemented-plans/index.md` (on implementation).
- In the library: `README.md` (what it is, the four contract surfaces, swarm deploy), the security model (moved from the parser-wrapper plan), the
  environment table, and how to write a processor.
