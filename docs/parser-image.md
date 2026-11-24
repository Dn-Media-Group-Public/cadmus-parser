# How to build the parser image

The parser service (the wrapper, not your service) runs in its own image. It needs no base image from this library: add the library source and run its installer, on any Debian/Ubuntu base with `python3`. `install.sh` sets up what the wrapper needs at runtime: `tini`, `util-linux` (`setpriv`), the `wrapper` uid 900 with `/scratch`, and a venv with the server installed.

```dockerfile
FROM debian:bookworm-slim
ENV PATH=/venv/bin:$PATH
ADD lib/cadmus-parser /cadmus
RUN apt-get update && apt-get install -y --no-install-recommends unar libmagic1 libimage-exiftool-perl ffmpeg flac tesseract-ocr tesseract-ocr-eng tesseract-ocr-nor && rm -rf /var/lib/apt/lists/* \
 && /cadmus/server/install.sh s3,files            # tini, util-linux, the `wrapper` uid, /scratch, a venv /venv, pip install of the server with these extras
COPY toy/ /src/toy/
RUN pip install --no-cache-dir /src/toy           # a processor package of your own (or only the default ones, from the `files` extra)
RUN pip install --no-cache-dir "/cadmus/server[gcs]" && chmod -R a+rX /venv     # GCS support, AFTER the processors (see below)
VOLUME /scratch
EXPOSE 8080
ENTRYPOINT ["tini", "--", "/cadmus/server/entrypoint.sh"]
```

- `media-decode` (in `files`) needs `ffmpeg` with libx264 and libopus (Debian's has both) and installs `pillow-heif` (HEIC; a wheel that bundles libheif).
  See also: [Plan: media decode](implemented-plans/02-media-decode.md)
- The system tools of the default processors: `unar` (rar and other archives), `exiftool`, `ffmpeg`/`ffprobe`, `libmagic1` (file type detection through `python-magic`, with its magic database), `flac` (markitdown's audio converter), `tesseract-ocr` plus one language pack per code in `OCR_LANGS` (default `nor+eng`). `server/Dockerfile` installs all of them; a missing tool degrades silently to a note in `status.json`.
- `install.sh [extras]` takes the server's extras (`s3` default, `files` = the default processors, `gcs`); the source stays in `/cadmus` for later `pip install`s.
- Install GCS support **after** the processors: `blackboxprotobuf` (used by `files`) pins a protobuf that `gcsfs` refuses, and pip then only warns.
- The venv must stay world-readable (`chmod -R a+rX /venv`): the parser runs as a pool uid, not as the wrapper's uid.
- Scratch keeps every intermediate until the job ends: size `PARSE_SCRATCH` for the input plus the kept intermediates of the largest job (the per-job limits still bound it).
  See also: [Plan: processor handoff](implemented-plans/01-processor-handoff.md)
- `/scratch` is an anonymous volume: the scratch area is ephemeral, so a restart deletes what a crash left.
- Sidecars are used as built by the library: `docker build -f blob-sidecar/Dockerfile .`, `docker build -f ocr-sidecar/Dockerfile .`, `docker build -f asr-sidecar/Dockerfile .` (the library root is the context). `server/Dockerfile`
  builds a ready-made image with the default processors and their tools (the quickstart uses it).
- Point the stack at your image with the overlay: `python -m cadmus_parser_server.overlay --client web --parser-image myorg/my-parser:1`.
