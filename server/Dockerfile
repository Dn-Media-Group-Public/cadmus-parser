# A ready-made parser image with NO processor (a consumer normally builds its own: see docs/parser-image.md).
# Build context: the repo root (docker build -f server/Dockerfile .).
FROM debian:bookworm-slim
ENV PATH=/venv/bin:$PATH
ADD . /cadmus
RUN /cadmus/server/install.sh s3
# the scratch area is ephemeral: an anonymous volume of the task (never a named one), so a restart deletes what a crash left
VOLUME /scratch
EXPOSE 8080
ENTRYPOINT ["tini", "--", "/cadmus/server/entrypoint.sh"]
