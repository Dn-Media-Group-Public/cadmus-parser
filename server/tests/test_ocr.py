"""OCR (docs/implemented-plans/ocr-hybrid.md). pytesseract and the sidecar are faked: no binary, no network."""

import io
import os

import pytest

pytest.importorskip("PIL")
from PIL import Image

from cadmus_parser_server import ocr


def png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(b, "PNG")
    return b.getvalue()


def fake_tess(monkeypatch, words, conf):
    pt = pytest.importorskip("pytesseract")
    d = {"text": words, "conf": [conf] * len(words), "block_num": [1] * len(words), "par_num": [1] * len(words),
         "line_num": [1] * len(words)}
    monkeypatch.setattr(pt, "image_to_data", lambda *a, **k: d)


class Ctx:
    def options_of(self, name):
        return {}


class Notes:
    def __init__(self, notes):
        self.note = notes.append


def run(notes):
    from cadmus_parser_server import page_ocr
    return page_ocr.plain_image_text(png(), "a.png", Ctx(), Notes(notes))


def test_confident_tesseract_wins(monkeypatch):
    fake_tess(monkeypatch, ["Hello", "wonderful", "world", "again"], 91)
    monkeypatch.delenv("OCR_URL", raising=False)
    n = []
    assert run(n) == "Hello wonderful world again" and n == ["a.png: ocr tesseract (conf 91)"]


def test_weak_result_kept_without_sidecar(monkeypatch):
    fake_tess(monkeypatch, ["x"], 38)
    monkeypatch.delenv("OCR_URL", raising=False)
    n = []
    assert run(n) == "x" and n == ["a.png: ocr tesseract (conf 38)"]


def test_weak_result_goes_remote_and_wins(monkeypatch):
    fake_tess(monkeypatch, ["x"], 38)
    monkeypatch.setenv("OCR_URL", "http://s")
    monkeypatch.setenv("OCR_TOKEN", "t")
    monkeypatch.setattr(ocr, "remote_ocr", lambda img, ct="image/png", tags=None: "remote text")
    n = []
    assert run(n) == "remote text" and n == ["a.png: ocr remote"]


def test_remote_failure_keeps_tesseract(monkeypatch):
    fake_tess(monkeypatch, ["x"], 38)
    monkeypatch.setenv("OCR_URL", "http://s")
    monkeypatch.setenv("OCR_TOKEN", "t")

    def boom(*a, **k):
        raise TimeoutError

    monkeypatch.setattr(ocr, "remote_ocr", boom)
    n = []
    assert run(n) == "x" and n == ["a.png: ocr tesseract (conf 38), remote failed: TimeoutError"]


def test_oversize_image_noted_not_failed(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    n = []
    assert run(n) == "" and "too large" in n[0]


@pytest.mark.skipif(not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "..", "ocr-sidecar")), reason="ocr-sidecar/ not next to server/")
def test_ocr_client_talks_to_the_sidecar(monkeypatch):
    """ocr.py against the real sidecar app (upstream faked), with the variables the wrapper would hand a parser: the common sidecar protocol."""
    import socket
    import sys
    import threading
    import time

    import httpx
    import uvicorn

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "ocr-sidecar"))
    import app as sidecar

    prof = {"p": {"token": "tk", "base_url": "http://up/v1", "model": "m"}}
    up = httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "OCR text"}}]}))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(sidecar.make_app(prof, transport=up), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    try:
        monkeypatch.delenv("OCR_URL", raising=False)
        assert not ocr.enabled()
        monkeypatch.setenv("OCR_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("OCR_TOKEN", "tk")
        assert ocr.enabled() and ocr.remote_ocr(b"\x89PNG") == "OCR text"
        monkeypatch.setenv("OCR_TOKEN", "other")
        with pytest.raises(Exception):
            ocr.remote_ocr(b"\x89PNG")
    finally:
        srv.should_exit = True


class Sink:
    def __init__(self):
        self.notes, self.files, self.infos = [], {}, []

    def note(self, m): self.notes.append(m)
    def info(self, **k): self.infos.append(k)
    def put_bytes(self, p, b, ct): self.files[p] = b


def documents(monkeypatch, tmp_path, words):
    from types import SimpleNamespace as NS
    from cadmus_parser_server import extract, parse

    f = tmp_path / "a.png"
    f.write_bytes(png())
    monkeypatch.setattr(parse, "_ctype", lambda file, ctx: "image/png")
    monkeypatch.setattr(extract, "file_to_markdown", lambda *a: pytest.fail("markitdown must not run for images"))
    monkeypatch.setattr(ocr, "ocr_engine", lambda img, langs=None: (words, None, "fake", "fake"))
    s = Sink()
    parse.Documents().process(s, NS(path=str(f), rel="a.png", size=1), Ctx())
    return s


def test_image_without_text_writes_nothing(monkeypatch, tmp_path):
    s = documents(monkeypatch, tmp_path, "")
    assert s.files == {} and s.infos == [] and s.notes == ["a.png: ocr fake"]


def test_image_with_text_writes_md(monkeypatch, tmp_path):
    assert documents(monkeypatch, tmp_path, "Hello").files["parsed/a.png.md"] == b"Hello"
