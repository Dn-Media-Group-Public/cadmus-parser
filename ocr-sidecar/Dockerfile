# The OCR sidecar: stateless forwarder to the configured OpenAI-compatible profiles, own pinned egress.
# Build context: the repo root (docker build -f ocr-sidecar/Dockerfile .)
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv iptables util-linux tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /venv && /venv/bin/pip install --no-cache-dir uvicorn starlette httpx google-auth requests \
 && useradd --system --no-create-home ocr
ENV PATH=/venv/bin:$PATH
COPY common/egress_rules.py ocr-sidecar/app.py ocr-sidecar/start.py /app/
EXPOSE 8080
ENTRYPOINT ["tini", "--", "python", "/app/start.py"]
