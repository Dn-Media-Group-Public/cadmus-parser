"""OCR (docs/implemented-plans/ocr-hybrid.md). pytesseract and the sidecar are faked: no binary, no network."""

import io

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


def run(notes):
    return ocr.maybe_ocr(png(), "a.png", "image/png", "EXIF", notes.append)


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
    monkeypatch.setattr(ocr, "remote_ocr", lambda img, ct="image/png": "remote text")
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
    assert run(n) == "EXIF" and "too large" in n[0]


def test_pdf_only_scanned_page_is_ocrd(monkeypatch):
    pdfium = pytest.importorskip("pypdfium2")
    pdf = pdfium.PdfDocument.new()
    for _ in range(2):
        pdf.new_page(100, 100)
    b = io.BytesIO()
    pdf.save(b)
    texts = iter(["A text layer long enough to count.", ""])
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_range", lambda self, *a, **k: next(texts))
    monkeypatch.setattr(ocr, "ocr_image", lambda img: ("scanned words", "tesseract (conf 90)"))
    n = []
    out = ocr.maybe_ocr(b.getvalue(), "d.pdf", "application/pdf", "md", n.append)
    assert out.index("<!-- page 1 -->") < out.index("<!-- page 2 (ocr) -->") and out.endswith("scanned words")
    assert n == ["d.pdf: page 2 ocr tesseract (conf 90)"]
    monkeypatch.setattr(ocr, "MAX_PAGES", 0)
    texts = iter(["", ""])
    n = []
    ocr.maybe_ocr(b.getvalue(), "d.pdf", "application/pdf", "md", n.append)
    assert n == ["d.pdf: ocr stopped at page 0"]
