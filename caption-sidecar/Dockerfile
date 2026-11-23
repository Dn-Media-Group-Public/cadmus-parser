# The caption sidecar: stateless forwarder to the configured OpenAI-compatible vision chat profiles, own pinned egress.
# Build context: the repo root (docker build -f caption-sidecar/Dockerfile .)
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv iptables util-linux tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /venv && /venv/bin/pip install --no-cache-dir uvicorn starlette httpx google-auth requests \
 && useradd --system --no-create-home caption
ENV PATH=/venv/bin:$PATH
COPY common/egress_rules.py common/usage.py caption-sidecar/app.py caption-sidecar/start.py /app/
EXPOSE 8080
ENTRYPOINT ["tini", "--", "python", "/app/start.py"]
