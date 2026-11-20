import os
import shutil
import tempfile

import pytest

from cadmus_parser_server import parse, processors, worker


class MemSink(worker.Sink):
    """Collects what the worker would hand to the wrapper, over all input files."""

    def __init__(self):
        self.files, self.text, self.notes, self.info_by = {}, set(), [], {}

    def for_file(self, address):
        outer, rel = self, ".d/".join(address)

        class Per(worker.Sink):
            def put_bytes(self, r, data, ctype, meta=None):
                outer.files[r] = data

            def put_file(self, r, src, ctype, meta=None):
                outer.files[r] = open(src, "rb").read()
                os.unlink(src)

            def info(self, content_type=None, meta=None, size=None):
                outer.info_by.setdefault(rel, {}).update(content_type=content_type, size=size)
                if (meta or {}).get("text"):
                    outer.text.add(f"original/{rel}")

            def note(self, text):
                outer.notes.append(text)

        return Per()


@pytest.fixture
def run():
    """run({name: bytes | (bytes, ctype)}, base_url="") -> MemSink: parse_job over a fresh input tree."""
    dirs = []

    def go(files: dict, base_url: str = "") -> MemSink:
        d = tempfile.mkdtemp()
        dirs.append(d)
        ctypes = {}
        for name, v in files.items():
            data, ct = v if isinstance(v, tuple) else (v, "application/octet-stream")
            ctypes[name] = ct
            os.makedirs(os.path.dirname(f"{d}/in/tree/{name}"), exist_ok=True)
            open(f"{d}/in/tree/{name}", "wb").write(data)
        os.makedirs(f"{d}/work")
        sink = MemSink()
        worker.Runner(sink, processors.order([c() for c in parse.PROCESSORS]), {"files": {"base_url": base_url, "ctypes": ctypes}}, [], f"{d}/work").run_tree(f"{d}/in/tree")
        return sink

    yield go
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_metadata(request, monkeypatch):
    """Metadata extraction needs exiftool/ffprobe and adds files and notes: only tests marked `metadata` run it."""
    if "metadata" not in request.keywords:
        from cadmus_parser_server import metadata
        monkeypatch.setattr(metadata, "extract_metadata", lambda *a, **k: {})


@pytest.fixture
def service():
    from harness import Service
    made = []

    def make(**cfg):
        made.append(Service(**cfg))
        return made[-1]

    yield make
    for s in made:
        s.stop()
