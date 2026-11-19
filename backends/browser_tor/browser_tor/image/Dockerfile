# browser_tor: the browser gateway base image plus this backend's package. Build context: the repo root
# (docker build -f backends/browser_tor/browser_tor/image/Dockerfile .).
ARG BASE_IMAGE=spider-hoarder-browser-base:local
FROM ${BASE_IMAGE}
COPY backends/browser_tor/ /src/browser_tor/
RUN pip install --no-cache-dir --no-deps /src/browser_tor && rm -rf /src && chmod -R a+rX /venv/lib
