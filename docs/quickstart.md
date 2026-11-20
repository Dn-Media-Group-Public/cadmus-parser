# Quickstart: a new client app

You write a **client**: a service that hands files to cadmus and reads back what it made of them. Here it is end to end: its Dockerfile, its `docker-compose.yml` (a swarm
stack file; that is only the conventional file name) and the Python that calls cadmus. You need Docker Swarm (`docker swarm init`) and an S3-compatible bucket; the example
uses `http://store:9000`.

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

```yaml
version: "3.8"
services:
  app:
    image: myapp:local
    environment:
      STORE_BUCKET: bucket
      STORE_ENDPOINT: http://store:9000
      STORE_KEY: ${STORE_KEY}
      STORE_SECRET: ${STORE_SECRET}
    networks: [public]        # the cadmus overlay adds the private network to the parser, PARSE_URL and PARSE_TOKEN
networks:
  public: { driver: overlay }
```

## app.py

```python
import os

import fsspec
from cadmus_parser import Client, mint, renew_loop

endpoint = os.environ["STORE_ENDPOINT"]
store = {"key": os.environ["STORE_KEY"], "secret": os.environ["STORE_SECRET"], "endpoint_url": endpoint}   # your own identity: minting needs IAM rights
base = f"s3://{os.environ['STORE_BUCKET']}/jobs/1"
inp, out = f"{base}/in", f"{base}/out"

# 1. put the files to parse under the input prefix
fs = fsspec.filesystem("s3", **store)
fs.pipe_file(f"{inp[5:]}/hello.txt", b"hello, cadmus")

# 2. credentials for exactly these two prefixes, nothing else (revoked when the block ends)
client = Client(os.environ["PARSE_URL"], os.environ["PARSE_TOKEN"], store_options={"endpoint_url": endpoint})
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

Build the app and the two cadmus images, generate the overlay (it joins your `app` to the parser on a private network and gives it `PARSE_URL` and `PARSE_TOKEN`), deploy:

```sh
docker build -t myapp:local .
docker build -f lib/cadmus-parser/server/Dockerfile -t cadmus-parser:local lib/cadmus-parser
docker build -f lib/cadmus-parser/blob-sidecar/Dockerfile -t cadmus-parser-blob-sidecar:local lib/cadmus-parser
docker run --rm cadmus-parser:local python -m cadmus_parser_server.overlay --client app > parse-overlay.yml

export WRAPPER_TOKEN=$(openssl rand -hex 32) PROXY_TOKEN=$(openssl rand -hex 32)
export PARSE_BLOB_OPTIONS='{"endpoint_url": "http://store:9000"}'      # no secrets: credentials come with each job
export STORE_KEY=... STORE_SECRET=...
docker stack deploy -c docker-compose.yml -c lib/cadmus-parser/docker-compose.parse.yml -c parse-overlay.yml mystack
docker service logs -f mystack_app
```

If the store runs inside your own stack, add `--dev-store host:port` to the overlay command: the egress proxy may then reach exactly that address and port.
