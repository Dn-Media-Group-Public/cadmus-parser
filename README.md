# cadmus-parser

Turn the files you did not write into Markdown, JSON and metadata, **without trusting the code that reads them**. Documents, web pages, images, archives, audio and
video go in; text, outlines and structured data come out, as artefacts in your own object store.

## What it can parse (the default `files` processors)

- **Everything to Markdown** with [markitdown](https://github.com/microsoft/markitdown): Word, Excel, PowerPoint, PDF, EPUB, CSV, JSON, XML, plain text ...
- **Web pages**: HTML to Markdown with main-content extraction ([trafilatura](https://trafilatura.readthedocs.io/)), links made absolute from a base URL, plus a simplified main-content view
- **Terse outline view** of every parsed document (headings, links, images) for cheap skimming
- **OCR**: images and scanned PDF pages, Tesseract first, a remote multimodal model as the fallback for poor results; also the frames of a video (`frame-ocr`)
- **Speech to text (ASR)**: the audio of audio and video files, transcribed in chunks and merged into one transcript, primary model with a fallback profile ([ASR](docs/reference.md#asr))
- **Captioning**: a description of each image and of windows of keyframes from a video, by a remote vision model with a fallback profile ([caption](docs/reference.md#caption))
- **Archives** (zip, tar, tgz, 7z, rar, gz/bz2/xz) are unpacked and every member parsed
- **Binary structured formats** (msgpack, CBOR, BSON, plist, protobuf, gRPC-web) become JSON
- **Media decode**: image, video and audio decoded once into a thumbnail, scene-change keyframes, a 16 kHz `audio.opus`, a browser-playable rendition (when the original does not play) and a downscaled frame store for later per-frame analysis ([options](docs/processors.md#media-decode))
- **File metadata** as YAML: EXIF/GPS, Office tracked changes and comments, GeoTIFF/GDAL and vector (GPX, KML, GeoJSON, GeoPackage, shapefile) CRS and bounds, audio/video streams and chapters, PDF edit history
- **File type detection** from content (libmagic), refined by file name only where the bytes are generic
- **Your own formats**: add a processor package; processors are ordered, run per file and can recurse into archives ([how](docs/processors.md))

Also: a per-input `index.json` that says what came out of each file and archive member; any fsspec store; horizontally scalable (each replica has its own uid pool); one instance per
consuming stack.

## Remote models

OCR fallback, ASR and captioning call remote models, always through a sidecar.

A **profile** is a named, operator-configured way of reaching one model: the endpoint, the model, how to authenticate, and a prompt, all kept in the sidecar. A job asks for a
profile by name and never sees or sets the details. Examples: `lan` (a self-hosted OCR server on the local network), a Whisper-style `transcriptions` endpoint for ASR, a
`chat` model with audio input as the ASR fallback, or a hosted vision model for captions.

- **You choose the profile per job.** `features` in the job (`{"asr": "<profile>", "asr-fallback": "<profile>", "caption": "<profile>", "ocr": "<profile>"}`) names which
  model profile each feature uses, and a fallback can be a different one; the parser gets only the profile its job asked for. Profiles (endpoint, model, auth, prompt) are
  defined by the operator in the sidecar, so a client can pick among them but not invent one. ([reference](docs/reference.md#asr), [sidecar protocol](docs/sidecar-protocol.md))
- **Usage is logged by the sidecar, not the parser.** One record per upstream call (feature, profile, model, outcome, tokens or audio seconds) ends up in the job's
  `status.json`, so cost data can be trusted even if the parser is not. ([status.json](docs/reference.md#statusjson-and-indexjson), [client guide](docs/client.md))

---

## Safe by construction

Run untrusted-file processing safely in a Docker Swarm stack.

Anything that interprets a file you did not write (documents, images, archives, audio, video) is a place where a parser bug becomes a break-in. cadmus-parser
runs that code as a process that **holds no credential and has no route out**: a small trusted wrapper moves the bytes between your object store and a
sandboxed parser on a pool uid, behind an authenticated egress proxy, with an optional sidecar for remote services (OCR, say) the parser may use without ever
holding their keys.

Defences beyond the sandbox itself:

- **Remote services** (OCR, ASR, captioning) are reached only through sidecars, so the parser never holds the endpoint's key.
- **Archives** are unpacked with bomb and zip-slip limits.
- **Per-media-class limits** (deadline, memory, file and tree sizes) are set by the operator only, never by the client or the file.
- **Store credentials** are job-scoped and renewable (S3 and GCS), so a long video does not outlive its access and a job can touch only its own data.

Each consuming stack runs **its own instance** of these services (own store, own tokens). What is shared is code and images, not a running service.
Only Docker Swarm stacks are supported (`docker stack deploy`).

## Threat model

The adversary is **the file, and so the parser** that reads it. The client (your service) is trusted, and so is the wrapper.

| | Protected | Not protected |
|---|---|---|
| The parser process | no credential, token, store URL or environment secret (it gets only settings, and the one sidecar profile its job asked for); a pool uid with no capabilities, `no_new_privs`, no readable files outside its job, rlimits and a wall-clock deadline per media class; internal-only networks, so no route out; wiped and verified after every job | a bug in the kernel or the container runtime |
| What it can write | only below the output base, under top-level directories the installed processors declare, as regular files (`O_NOFOLLOW`), sizes from the wrapper's own `fstat`, record, line and total-byte caps | |
| What it can read | only the input tree the wrapper pulled | |
| The wrapper API | one bearer token (the client's) | a stolen token (see the [security guide](docs/security.md)) |
| The stores | whatever the credential the client hands over for each job allows: mint it job-scoped ([client guide](docs/client.md)) | a client that builds URLs or options from untrusted input, or hands over a broad credential |

## Who is who

- **Client**: a service written by someone else (a web app, say) that uses cadmus. Its image is built from anything (Debian ...) with `ADD lib/cadmus-parser /cadmus` and
  `RUN pip install /cadmus/client`; it calls the wrapper over REST with the `cadmus_parser` Python library. ([client image](docs/client-image.md))
- **Wrapper** (`cadmus_parser_server`): the privileged, processor-independent Python code that copies data between blob storage and the processors.
- **Processor**: untrusted Python code that does the parsing. It runs in the wrapper's container, as a different (pool) user with nothing. (What the rest of these docs call
  "the parser" is the processor process.) ([how to write one](docs/processors.md), [the parser image](docs/parser-image.md))

## Parts

| Part | What it is |
|---|---|
| `server/` (`cadmus-parser-server`) | the wrapper (HTTP API, uid pool, upload, `status.json`/`index.json`), the worker (the sandboxed process), the processor registry, the default processors (the `files` extra) and the stack-file overlay generator |
| `client/` (`cadmus-parser`) | what a client uses: `Client` (post a job, wait, read the index), `mint` (job-scoped store credentials) and `renew_loop` (keeps them alive during long jobs) |
| `blob-sidecar/` | the authenticated egress proxy (squid) the wrapper reaches the store through; public internet only |
| `caption-sidecar/` | one image or window of keyframes to one upstream vision-model call, JSON answer; the processors in `server/` window, validate and fall back ([caption](docs/reference.md#caption)) |
| `asr-sidecar/` | one audio chunk to one upstream ASR call (`transcriptions` or `chat` backend), JSON answer; the processors in `server/` chunk, validate, fall back and merge ([ASR](docs/reference.md#asr)) |
| `ocr-sidecar/` | a forwarder to OpenAI-compatible OCR endpoints, one token per profile; the model for [your own sidecar](docs/sidecar-protocol.md) |
| `docker-compose.parse.yml` | the swarm stack file: `parser`, `blob-sidecar`, networks and capabilities |
| `server/install.sh`, `server/Dockerfile` | what the **parser image** (wrapper plus processors) runs after `ADD lib/cadmus-parser /cadmus` (see [the parser image](docs/parser-image.md)); the Dockerfile is a ready-made one with the default processors |

## Documentation

- [Quickstart](docs/quickstart.md): a minimal client stack and a toy processor, end to end
- [Reference](docs/reference.md): wrapper HTTP API, `index.json`, processor entry point, stack file, environment variables, `PARSE_LIMITS`
- [How to write a processor](docs/processors.md) · [How to build a client image](docs/client-image.md) · [the parser image](docs/parser-image.md) · [How to write a sidecar](docs/sidecar-protocol.md)
- [Client and credential minting](docs/client.md)
- [Security guide](docs/security.md) · [Versioning and upgrades](docs/versioning.md)

## Tests

`pip install "./server[s3,files,test]" "./client[test]" pytest-asyncio httpx` then, one process per area: `python -m pytest server`, `client`, `blob-sidecar`, `ocr-sidecar`, `asr-sidecar`, `caption-sidecar`.
The tests use `file://` stores; nothing needs a running object store.

MIT licence.
