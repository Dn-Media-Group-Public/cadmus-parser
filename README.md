# cadmus-parser

Turn the files you did not write into Markdown, JSON and metadata, **without trusting the code that reads them**. Documents, web pages, images, archives, audio and
video go in; text, outlines and structured data come out, as artefacts in your own object store.

## What it can parse (the default `files` processors)

- **Everything to Markdown** with [markitdown](https://github.com/microsoft/markitdown): Word, Excel, PowerPoint, PDF, EPUB, CSV, JSON, XML, plain text ...
- **Web pages**: HTML to Markdown with main-content extraction ([trafilatura](https://trafilatura.readthedocs.io/)), links made absolute from a base URL, plus a simplified main-content view
- **Terse outline view** of every parsed document (headings, links, images) for cheap skimming
- **OCR**: images and scanned PDF pages, Tesseract first, a remote multimodal model as the fallback for poor results (through a sidecar, so the parser never holds the endpoint's key)
- **Archives** (zip, tar, tgz, 7z, rar, gz/bz2/xz) are unpacked and every member parsed, with the usual bomb and zip-slip limits
- **Binary structured formats** (msgpack, CBOR, BSON, plist, protobuf, gRPC-web) become JSON
- **File metadata** as YAML: EXIF/GPS, Office tracked changes and comments, GeoTIFF/GDAL and vector (GPX, KML, GeoJSON, GeoPackage, shapefile) CRS and bounds, audio/video streams and chapters, PDF edit history
- **Your own formats**: add a processor package; processors are ordered, run per file and can recurse into archives ([how](docs/processors.md))

Other consumer-facing capabilities: per-media-class limits (deadline, memory, file and tree sizes) set by the operator only; a per-input `index.json` that says what came out of
each file and archive member; job-scoped, renewable store credentials (S3 and GCS) so a long video does not outlive its access; any fsspec store; horizontally scalable
(each replica has its own uid pool); one instance per consuming stack.

---

## Safe by construction

Run untrusted-file processing safely in a Docker Swarm stack.

Anything that interprets a file you did not write (documents, images, archives, audio, video) is a place where a parser bug becomes a break-in. cadmus-parser
runs that code as a process that **holds no credential and has no route out**: a small trusted wrapper moves the bytes between your object store and a
sandboxed parser on a pool uid, behind an authenticated egress proxy, with an optional sidecar for remote services (OCR, say) the parser may use without ever
holding their keys.

Each consuming stack runs **its own instance** of these services (own store, own tokens). What is shared is code and images, not a running service.
Only Docker Swarm stacks are supported (`docker stack deploy`).

## Threat model

The adversary is **the file, and so the parser** that reads it. The consumer (your service) is trusted, and so is the wrapper.

| | Protected | Not protected |
|---|---|---|
| The parser process | no credential, token, store URL or environment secret (it gets only settings, and the one sidecar profile its job asked for); a pool uid with no capabilities, `no_new_privs`, no readable files outside its job, rlimits and a wall-clock deadline per media class; internal-only networks, so no route out; wiped and verified after every job | a bug in the kernel or the container runtime |
| What it can write | only below the output base, under top-level directories the installed processors declare, as regular files (`O_NOFOLLOW`), sizes from the wrapper's own `fstat`, record, line and total-byte caps | |
| What it can read | only the input tree the wrapper pulled | |
| The wrapper API | one bearer token (the consumer's) | a stolen token (see the [security guide](docs/security.md)) |
| The stores | whatever the credential the consumer hands over for each job allows: mint it job-scoped ([client guide](docs/client.md)) | a consumer that builds URLs or options from untrusted input, or hands over a broad credential |

## Parts

| Part | What it is |
|---|---|
| `server/` (`cadmus-parser-server`) | the wrapper (HTTP API, uid pool, upload, `status.json`/`index.json`), the worker (the sandboxed process), the processor registry, the default processors (the `files` extra) and the stack-file overlay generator |
| `client/` (`cadmus-parser`) | what a consumer uses: `Client` (post a job, wait, read the index), `mint` (job-scoped store credentials) and `renew_loop` (keeps them alive during long jobs) |
| `blob-sidecar/` | the authenticated egress proxy (squid) the wrapper reaches the store through; public internet only |
| `ocr-sidecar/` | a forwarder to OpenAI-compatible OCR endpoints, one token per profile; the model for [your own sidecar](docs/sidecar-protocol.md) |
| `docker-compose.parse.yml` | the swarm stack file: `parser`, `blob-sidecar`, networks and capabilities |
| `server/Dockerfile` | the **base image** `cadmus-parser-base`: wrapper, Python, tini; no processor and no format tools |

## Documentation

- [Quickstart](docs/quickstart.md): a minimal consumer stack and a toy processor, end to end
- [Reference](docs/reference.md): wrapper HTTP API, `index.json`, processor entry point, stack file, environment variables, `PARSE_LIMITS`
- [How to write a processor](docs/processors.md) · [How to build a consumer image](docs/consumer-image.md) · [How to write a sidecar](docs/sidecar-protocol.md)
- [Client and credential minting](docs/client.md)
- [Security guide](docs/security.md) · [Versioning and upgrades](docs/versioning.md)

## Tests

`pip install "./server[s3,files,test]" "./client[test]" pytest-asyncio httpx` then, one process per area: `python -m pytest server`, `client`, `blob-sidecar`, `ocr-sidecar`.
The tests use `file://` stores; nothing needs a running object store.

MIT licence.
