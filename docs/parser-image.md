# How to build the parser image

The parser service (the wrapper, not your service) runs in its own image. It needs no base image from this library: add the library source and run its installer, on any Debian/Ubuntu base with `python3`. `install.sh` sets up what the wrapper needs at runtime: `tini`, `util-linux` (`setpriv`), the `wrapper` uid 900 with `/scratch`, and a venv with the server installed.

```dockerfile
FROM debian:bookworm-slim
ENV PATH=/venv/bin:$PATH
ADD lib/cadmus-parser /cadmus
RUN apt-get update && apt-get install -y --no-install-recommends libimage-exiftool-perl ffmpeg && rm -rf /var/lib/apt/lists/* \
 && /cadmus/server/install.sh s3,files            # tini, util-linux, the `wrapper` uid, /scratch, a venv /venv, pip install of the server with these extras
COPY toy/ /src/toy/
RUN pip install --no-cache-dir /src/toy           # a processor package of your own (or only the default ones, from the `files` extra)
RUN pip install --no-cache-dir "/cadmus/server[gcs]" && chmod -R a+rX /venv     # GCS support, AFTER the processors (see below)
VOLUME /scratch
EXPOSE 8080
ENTRYPOINT ["tini", "--", "/cadmus/server/entrypoint.sh"]
```

- `install.sh [extras]` takes the server's extras (`s3` default, `files` = the default processors, `gcs`); the source stays in `/cadmus` for later `pip install`s.
- Install GCS support **after** the processors: `blackboxprotobuf` (used by `files`) pins a protobuf that `gcsfs` refuses, and pip then only warns.
- The venv must stay world-readable (`chmod -R a+rX /venv`): the parser runs as a pool uid, not as the wrapper's uid.
- `/scratch` is an anonymous volume: the scratch area is ephemeral, so a restart deletes what a crash left.
- Sidecars are used as built by the library: `docker build -f blob-sidecar/Dockerfile .`, `docker build -f ocr-sidecar/Dockerfile .` (the library root is the context). `server/Dockerfile`
  builds a ready-made image with no processor, for trying things out.
- Point the stack at your image with the overlay: `python -m cadmus_parser_server.overlay --consumer web --parser-image myorg/my-parser:1`.
