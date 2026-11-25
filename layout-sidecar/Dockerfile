# The layout sidecar: the `yolo-onnx` engine runs in it (CPU, onnxruntime only, no torch); `docling-serve` and `openai-vlm` profiles only call out. Own pinned egress.
# Build context: the repo root (docker build -f layout-sidecar/Dockerfile --build-arg MODEL_URL=... --build-arg MODEL_SHA256=... --build-arg MODEL_NAME=... --build-arg MODEL_LICENSE=... .)
# The weights are fetched here, at build time, from a pinned URL and checked against a pinned SHA-256: nothing is downloaded at runtime. `model_path` in a profile names
# /models/<file>; a different model is a different image tag.
FROM debian:trixie-slim AS weights
ARG MODEL_URL
ARG MODEL_SHA256
ARG MODEL_NAME=unknown
ARG MODEL_LICENSE=unknown
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN mkdir /models \
 && if [ -n "$MODEL_URL" ]; then curl -fsSL "$MODEL_URL" -o /models/model.onnx && echo "$MODEL_SHA256  /models/model.onnx" | sha256sum -c -; fi \
 && printf 'model: %s\nlicence: %s\nurl: %s\nsha256: %s\n' "$MODEL_NAME" "$MODEL_LICENSE" "$MODEL_URL" "$MODEL_SHA256" > /models/README

FROM debian:trixie-slim
ARG MODEL_NAME=unknown
ARG MODEL_LICENSE=unknown
LABEL org.opencontainers.image.model.name="$MODEL_NAME" org.opencontainers.image.model.licenses="$MODEL_LICENSE"
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv iptables util-linux tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /venv && /venv/bin/pip install --no-cache-dir uvicorn starlette httpx google-auth requests numpy pillow onnxruntime \
 && useradd --system --no-create-home layout
ENV PATH=/venv/bin:$PATH
COPY --from=weights /models /models
COPY common/egress_rules.py common/usage.py common/upstream_auth.py layout-sidecar/app.py layout-sidecar/start.py /app/
EXPOSE 8080
ENTRYPOINT ["tini", "--", "python", "/app/start.py"]
