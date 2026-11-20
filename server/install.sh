#!/bin/sh
# Installs the parser service into the image being built, on any Debian/Ubuntu base that has python3 (python3-venv, pip): tini and util-linux
# (setpriv), the `wrapper` uid 900 and its /scratch, a venv /venv with cadmus-parser-server (extras from $1, default `s3`; e.g. `s3,files`).
# Run it from a client's Dockerfile after `ADD lib/cadmus-parser /cadmus`:   RUN /cadmus/server/install.sh s3,files
# The image then needs ENV PATH=/venv/bin:$PATH and ENTRYPOINT ["tini", "--", "/cadmus/server/entrypoint.sh"]. A processor package of your own: RUN pip install /yours.
set -e
here=$(cd "$(dirname "$0")" && pwd)
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates tini util-linux
rm -rf /var/lib/apt/lists/*
python3 -m venv /venv
getent group wrapper >/dev/null || groupadd -g 900 wrapper
id wrapper >/dev/null 2>&1 || useradd -u 900 -g 900 --no-create-home --shell /usr/sbin/nologin wrapper
mkdir -p /scratch && chown wrapper:wrapper /scratch
/venv/bin/pip install --no-cache-dir "$here[${1:-s3}]"
chmod -R a+rX /venv /cadmus 2>/dev/null || chmod -R a+rX /venv "$here"
