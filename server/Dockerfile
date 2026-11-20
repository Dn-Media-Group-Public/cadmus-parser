# The base image of the parser container: the wrapper (own uid, keeps CAP_SETUID/SETGID/CHOWN/KILL) and the sandboxed parser processes it forks
# as pool uids. It holds NO processor and no format tools: a consumer's own Dockerfile starts FROM it, installs its processor package and the
# tools that package needs, e.g.
#     FROM ${IMAGE_PREFIX}cadmus-parser-base:${IMAGE_TAG}
#     RUN apt-get update && apt-get install -y --no-install-recommends libimage-exiftool-perl ffmpeg && rm -rf /var/lib/apt/lists/*
#     RUN pip install --no-cache-dir "/opt/cadmus-parser/server[files]"     # the default `files` processor, or your own package
# Build context: the repo root (docker build -f server/Dockerfile .). The source stays in /opt/cadmus-parser/server for that pip install.
FROM debian:bookworm-slim
ENV PATH=/venv/bin:$PATH
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates tini util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /venv \
 && groupadd -g 900 wrapper && useradd -u 900 -g 900 --no-create-home --shell /usr/sbin/nologin wrapper \
 && mkdir /scratch && chown wrapper:wrapper /scratch
COPY server/ /opt/cadmus-parser/server/
RUN pip install --no-cache-dir "/opt/cadmus-parser/server[s3,gcs]" && chmod -R a+rX /venv /opt/cadmus-parser
# the scratch area is ephemeral: an anonymous volume of the task (never a named one), so a restart deletes what a crash left
VOLUME /scratch
COPY server/entrypoint.sh /app/entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
