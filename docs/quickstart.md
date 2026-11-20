# Quickstart: a new client app

You write a **client**: a service that hands files to cadmus and reads back what it made of them. Here it is end to end: its Dockerfile, its `docker-compose.yml` (a swarm
stack file; that is only the conventional file name) and the Python that calls cadmus. You need Docker Swarm (`docker swarm init`) and an S3 bucket plus an identity that may create IAM
roles and assume them (that is how cadmus hands each job a credential limited to its own prefixes; AWS S3 here, any S3-compatible store with IAM and STS works, see the note at the end).

Project layout (`lib/cadmus-parser` is this repository, as a submodule or a plain clone):

```
myapp/
  lib/cadmus-parser/
  Dockerfile
  docker-compose.yml
  app.py
```

## Dockerfile

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-pip && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /venv
ENV PATH=/venv/bin:$PATH
ADD lib/cadmus-parser /cadmus
RUN pip install --no-cache-dir "/cadmus/client[mint-s3]" s3fs
COPY app.py /app/app.py
CMD ["python", "/app/app.py"]
```

## docker-compose.yml

Your stack file adds your service and joins it to the parser (`parser` is defined by the library's stack file, which is deployed together with this one and merged by service name).
The network is private to your app and the parser, so that several clients in one swarm cannot reach each other's parser.

```yaml
version: "3.8"
services:
  app:
    image: myapp:local
    environment:
      STORE_BUCKET: ${STORE_BUCKET}
      STORE_ENDPOINT: ${STORE_ENDPOINT:-}      # empty = AWS S3
      STORE_KEY: ${STORE_KEY}
      STORE_SECRET: ${STORE_SECRET}
      PARSE_URL: http://parser:8080
      PARSE_TOKEN: ${WRAPPER_TOKEN}
    networks: [app-parse]
  parser:
    networks: [app-parse]
networks:
  app-parse: { driver: overlay, internal: true }
```

## app.py

```python
import os

import fsspec
from cadmus_parser import Client, mint, renew_loop

store = {"key": os.environ["STORE_KEY"], "secret": os.environ["STORE_SECRET"]}      # your own identity: minting needs IAM rights
if os.environ.get("STORE_ENDPOINT"):
    store["endpoint_url"] = os.environ["STORE_ENDPOINT"]
base = f"s3://{os.environ['STORE_BUCKET']}/jobs/1"
inp, out = f"{base}/in", f"{base}/out"

# 1. put the files to parse under the input prefix
fs = fsspec.filesystem("s3", **store)
fs.pipe_file(f"{inp[5:]}/hello.txt", b"hello, cadmus")

# 2. credentials for exactly these two prefixes, nothing else (revoked when the block ends)
client = Client(os.environ["PARSE_URL"], os.environ["PARSE_TOKEN"], store_options={k: v for k, v in store.items() if k == "endpoint_url"})
grants = [(inp, "read"), (out, "write")]
with mint(grants, options=store) as creds:
    started = client.post(inp, out, creds)                                     # 202: the job runs
    loop = renew_loop(client, grants, started.id, started.owner, creds, options=store)   # keeps the credentials alive for long jobs
    try:
        status = client.wait(out, lambda: loop.current[out])                   # reads status.json from the store
        index = client.index(out, loop.current[out])                           # what came out of each input
    finally:
        loop.stop()

print(status)                                                                  # {'status': 'done', ...}
for name, entry in index["files"].items():
    print(name, entry["size"], list(entry["outputs"]))                         # hello.txt 13 ['parsed/hello.txt.md', ...]
```

`out/parsed/hello.txt.md` now holds the Markdown, `out/index.json` and `out/status.json` sit beside it. Your app never sends file bytes to cadmus and never holds more than the two
prefixes it asked for. [Client and minting reference](client.md) · [reference](reference.md).

## Deploy

```sh
docker build -t myapp:local .
docker build -f lib/cadmus-parser/server/Dockerfile -t cadmus-parser:local lib/cadmus-parser
docker build -f lib/cadmus-parser/blob-sidecar/Dockerfile -t cadmus-parser-blob-sidecar:local lib/cadmus-parser

export WRAPPER_TOKEN=$(openssl rand -hex 32) PROXY_TOKEN=$(openssl rand -hex 32)   # two different secrets
export STORE_BUCKET=my-bucket STORE_KEY=... STORE_SECRET=...                      # and STORE_ENDPOINT for a non-AWS store
docker stack deploy -c docker-compose.yml -c lib/cadmus-parser/docker-compose.parse.yml mystack
docker service logs -f mystack_app
```

`PARSE_BLOB_OPTIONS` (JSON fsspec options such as `{"endpoint_url": "..."}`, never secrets) is only needed for a non-AWS store.

**A store inside your own stack** (SeaweedFS with IAM enabled, say): the egress proxy blocks private addresses, so name the one exception in your stack file, next to a network the proxy shares with the store:

```yaml
  blob-sidecar:
    environment:
      PARSE_PROXY_ALLOW: seaweedfs:8333
    networks: [store]
```

With many clients, `python -m cadmus_parser_server.overlay --client app1 --client app2` prints the per-client networks and wiring above for all of them ([reference](reference.md#stack-file)).
