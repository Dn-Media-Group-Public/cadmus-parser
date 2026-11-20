"""A wrapper in a thread of the test process over `file://` stores: not root, so slots run as the test's own uid; the parser is a stand-in script
or the real worker. No object store and no consumer library: the library's tests stand alone."""

import json
import os
import socket
import sys
import tempfile
import threading
import time

import httpx
import uvicorn

from cadmus_parser_server import wrapper

TOKEN = "test-wrapper-token"
PROFILES = {"ocr": {"remote": {"url": "http://ocr-remote", "token": "tok-remote"}, "lan": {"url": "http://ocr-lan", "token": "tok-lan"}}}
TOP = {"original", "parsed", "simplified", "terse", "metadata"}
# the real worker minus the metadata extractors (they need exiftool and ffprobe, which dev machines lack)
NO_METADATA = [sys.executable, "-I", "-u", "-c", "import sys\nfrom cadmus_parser_server import metadata, worker\n"
               "metadata.extract_metadata = lambda *a, **k: {}\nworker.main(sys.argv[1])"]


def script(code: str) -> list:
    """A stand-in parser: `code` runs with fd 1 = the protocol pipe, jobdir = sys.argv[1]."""
    return [sys.executable, "-I", "-u", "-c", "import os, sys, json, time\njobdir = sys.argv[1]\n" + code]


def hostile(code: str) -> list:
    return script("os.makedirs(jobdir + '/out/parsed', exist_ok=True)\n" + code)


class Job:
    def __init__(self, svc: "Service", files: dict):
        self.svc, self.root = svc, tempfile.mkdtemp(dir=svc.store)
        self.input, self.output = f"file://{self.root}/in", f"file://{self.root}/out"
        for name, data in files.items():
            os.makedirs(os.path.dirname(f"{self.root}/in/{name}"), exist_ok=True)
            open(f"{self.root}/in/{name}", "wb").write(data)
        os.makedirs(f"{self.root}/out", exist_ok=True)

    def body(self, **extra) -> dict:
        return {"input": {"url": self.input}, "output": {"url": self.output}, **extra}

    def post(self, token=TOKEN, **extra):
        return httpx.post(f"{self.svc.url}/jobs", json=self.body(**extra), headers={"Authorization": f"Bearer {token}"})

    def read(self, name):
        return open(f"{self.root}/out/{name}", "rb").read()

    def status(self):
        try:
            return json.loads(self.read("status.json"))
        except FileNotFoundError:
            return None

    def index(self):
        return json.loads(self.read("index.json"))

    def wait(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            s = self.status()
            if s and s["status"] in ("done", "failed"):
                return s
            time.sleep(0.05)
        raise AssertionError("timeout")

    def outputs(self) -> list:
        return sorted(os.path.relpath(os.path.join(d, n), f"{self.root}/out") for d, _, ns in os.walk(f"{self.root}/out") for n in ns
                      if n not in ("index.json", "status.json"))


class Service:
    def __init__(self, **cfg):
        self.store = tempfile.mkdtemp()
        cfg.setdefault("profiles", PROFILES)
        cfg.setdefault("top_dirs", TOP)
        cfg.setdefault("blob_options", {"auto_mkdir": True})  # a local filesystem has directories; an object store does not need them
        self.cfg = wrapper.Config(TOKEN, scratch=tempfile.mkdtemp(), pool_size=cfg.pop("pool_size", 2), **cfg)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.server = uvicorn.Server(uvicorn.Config(wrapper.make_app(self.cfg), host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}"
        self.wrapper = self.server.config.app.state.wrapper

    def job(self, files: dict) -> Job:
        return Job(self, files)

    def stop(self):
        self.server.should_exit = True
