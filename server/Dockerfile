# A ready-made parser image (wrapper plus the default processors and every tool they use: unar, exiftool, ffmpeg, flac, Tesseract with the default OCR_LANGS nor+eng): enough to try cadmus, or to run as is. For your own processors
# or tools build your own: docs/parser-image.md. Build context: the repo root (docker build -f server/Dockerfile -t cadmus-parser:local .).
FROM debian:bookworm-slim
ENV PATH=/venv/bin:$PATH
ADD . /cadmus
RUN apt-get update \
 && apt-get install -y --no-install-recommends unar libimage-exiftool-perl ffmpeg flac tesseract-ocr tesseract-ocr-eng tesseract-ocr-nor \
 && rm -rf /var/lib/apt/lists/* \
 && /cadmus/server/install.sh s3,files
# GCS support after the processors: blackboxprotobuf pins a protobuf that gcsfs refuses (pip only warns)
RUN pip install --no-cache-dir "/cadmus/server[gcs]" && chmod -R a+rX /venv
# the scratch area is ephemeral: an anonymous volume of the task (never a named one), so a restart deletes what a crash left
VOLUME /scratch
EXPOSE 8080
ENTRYPOINT ["tini", "--", "/cadmus/server/entrypoint.sh"]
