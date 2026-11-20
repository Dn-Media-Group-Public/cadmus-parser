# How to build a consumer image

The library builds a **base image** `${IMAGE_PREFIX}cadmus-parser-base:${IMAGE_TAG}` from `server/Dockerfile` (context: the repo root): Debian, Python,
tini, util-linux, the wrapper, fsspec and the S3 store. It has **no processor and no format tools**. A consumer's Dockerfile starts `FROM` it, installs its
processor package and the tools that package needs, once per stack:

```dockerfile
ARG BASE_IMAGE=cadmus-parser-base:local
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends libimage-exiftool-perl ffmpeg && rm -rf /var/lib/apt/lists/*
COPY toy/ /src/toy/
RUN pip install --no-cache-dir /src/toy "/opt/cadmus-parser/server[files]"   # your processor and/or the default `files` processor
RUN pip install --no-cache-dir "/opt/cadmus-parser/server[gcs]"              # GCS support, after the processors (see below)
RUN chmod -R a+rX /venv
```

- The server's source stays in `/opt/cadmus-parser/server` for exactly this `pip install` (extras: `s3` is in the base, `gcs`, `files`).
- Install GCS support **after** the processors: `blackboxprotobuf` (used by `files`) pins a protobuf that `gcsfs` refuses, and pip then only warns.
- The image must stay world-readable (`chmod -R a+rX /venv`): the parser runs as a pool uid, not as the wrapper's uid.
- Build the base first: `docker build -f server/Dockerfile -t cadmus-parser-base:local .` in the library checkout, then your own image
  (feed `BASE_IMAGE` with the same prefix and tag you gave the base). The sidecars are used as built by the library
  (`docker build -f blob-sidecar/Dockerfile .`, `docker build -f ocr-sidecar/Dockerfile .`, both with the library root as context).
- Point the stack at your image with the overlay: `python -m cadmus_parser_server.overlay --consumer web --parser-image myorg/my-parser:1`.
