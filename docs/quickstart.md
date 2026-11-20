# Quickstart

A consumer stack with one service (`app`), the parser service and a toy processor, end to end. You need Docker Swarm and an S3-compatible bucket (the example
uses a store reachable at `http://store:9000`).

## 1. A processor

```python
# toy/toy/processor.py
class Upper:
    name = "upper"
    top_dirs = ("upper",)

    def accepts(self, file):
        return file.name.endswith(".txt")

    def process(self, sink, file, ctx):
        with open(file.path, "rb") as f:
            sink.put_bytes(f"upper/{file.rel}", f.read().upper(), "text/plain")
```

```toml
# toy/pyproject.toml
[project]
name = "toy"
version = "0.1.0"
[project.entry-points."cadmus_parser.processors"]
upper = "toy.processor:Upper"
```

## 2. Images

```sh
git clone https://github.com/Dn-Media-Group-Public/cadmus-parser lib/cadmus-parser     # or: git submodule add ... lib/cadmus-parser
docker build -f lib/cadmus-parser/blob-sidecar/Dockerfile -t cadmus-parser-blob-sidecar:local lib/cadmus-parser
```

```dockerfile
# parser/Dockerfile
FROM debian:bookworm-slim
ENV PATH=/venv/bin:$PATH
ADD lib/cadmus-parser /cadmus
RUN /cadmus/server/install.sh s3
COPY toy/ /src/toy/
RUN pip install --no-cache-dir /src/toy && chmod -R a+rX /venv
VOLUME /scratch
ENTRYPOINT ["tini", "--", "/cadmus/server/entrypoint.sh"]
```

`docker build -t myorg-parser:local -f parser/Dockerfile .` (see [How to build a consumer image](consumer-image.md)).

## 3. The stack

`app` is your service (any image with `pip install ./client`); it only needs `PARSE_URL` and `PARSE_TOKEN`, which the overlay adds.

```sh
pip install ./lib/cadmus-parser/server
python -m cadmus_parser_server.overlay --consumer app --parser-image myorg-parser:local > parse-overlay.yml
export WRAPPER_TOKEN=$(openssl rand -hex 32) PROXY_TOKEN=$(openssl rand -hex 32)
export PARSE_BLOB_OPTIONS='{"endpoint_url": "http://store:9000"}'     # no secrets: credentials come with each job
docker swarm init   # once
docker stack deploy -c docker-compose.yml -c lib/cadmus-parser/docker-compose.parse.yml -c parse-overlay.yml mystack
```

(`docker-compose.yml` is your own swarm stack file defining `app` and the network `public`; the name is just the conventional file name.) If your store runs inside the
stack, add `--dev-store host:port` to the overlay: the proxy may then reach exactly that address and port.

## 4. A job

```python
import os
from cadmus_parser import Client, mint, renew_loop

client = Client(os.environ["PARSE_URL"], os.environ["PARSE_TOKEN"], store_options={"endpoint_url": "http://store:9000"})
inp, out = "s3://bucket/jobs/1/in", "s3://bucket/jobs/1/out"
grants = [(inp, "read"), (out, "write")]
with mint(grants, options={"key": "...", "secret": "...", "endpoint_url": "http://store:9000"}) as creds:    # your own identity: it needs IAM rights
    # ... upload some .txt files under s3://bucket/jobs/1/in/ ...
    started = client.post(inp, out, creds)
    loop = renew_loop(client, grants, started.id, started.owner, creds)
    try:
        print(client.wait(out, lambda: loop.current[out]))                      # {"status": "done", ...}
        print(client.index(out, loop.current[out])["files"])                    # {"a.txt": {"size": 5, "outputs": {"upper/a.txt": {...}}}}
    finally:
        loop.stop()
```

`s3://bucket/jobs/1/out/upper/a.txt` now holds the upper-cased file, `index.json` and `status.json` sit beside it, and the parser that read `a.txt` never held a
credential or had a route out.
