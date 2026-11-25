"""Figure extraction (docs/implemented-plans/figure-extraction.md): languages, the layout chain client, `ocr_page` (gate, filtering, crops, full profiles) and the page units
end to end through the worker (Tesseract and the sidecars are faked)."""

import io
import json
import os
import shutil
import tempfile

import pytest

pytest.importorskip("PIL")
from PIL import Image

from cadmus_parser_server import layout, ocr, page_ocr, parse, processors, worker
from conftest import MemSink

TEXT = "word " * 60  # 300 characters: over the gate


def png_bytes(w=400, h=300, colour="white") -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(b, "PNG")
    return b.getvalue()


@pytest.fixture(autouse=True)
def fake_tesseract(monkeypatch):
    monkeypatch.setattr(ocr, "_tesseract", lambda img, lang=None: (TEXT, 95.0))
    monkeypatch.delenv("OCR_URL", raising=False)
    monkeypatch.delenv("LAYOUT_CHAIN", raising=False)


def chain(monkeypatch, *names):
    monkeypatch.setenv("LAYOUT_CHAIN", json.dumps([{"name": n, "url": "http://x", "token": "t"} for n in names]))


def answer(boxes, below=0.5, **kw):
    return {"engine": "yolo-onnx", "model": "m", "width": 400, "height": 300, "boxes": boxes, "escalate_below": below, "max_figure_members": 2000, **kw}


def pic(x0, y0, x1, y1, score=0.9, label="picture"):
    return {"label": label, "bbox": [x0, y0, x1, y1], "score": score}


# ---- languages ----------------------------------------------------------------------------------------------------

def test_languages_selected_default_and_dropped(monkeypatch):
    monkeypatch.setattr(ocr, "LANGS", "nor+eng+deu")
    monkeypatch.setattr(ocr, "DEFAULT_LANGS", "")
    n = []
    assert ocr.resolve_langs(None) == ("nor+eng+deu", [])  # nothing set: everything installed, no hint
    assert ocr.resolve_langs(["nb", "en", "nn"]) == ("nor+eng", ["nb", "en", "nn"])
    assert ocr.resolve_langs(["fr", "xx", "de"], n.append) == ("deu", ["de"]) and len(n) == 2  # fra not installed, xx unknown
    assert ocr.resolve_langs(["fr"], n.append) == ("nor+eng+deu", [])  # nothing left: as if unset
    monkeypatch.setattr(ocr, "DEFAULT_LANGS", "nb,en")
    assert ocr.resolve_langs(None) == ("nor+eng", ["nb", "en"]) and ocr.resolve_langs(["de"]) == ("deu", ["de"])  # the job overrides the default


# ---- the chain ------------------------------------------------------------------------------------------------------

def test_escalation_unsure_empty_and_failed_and_last_stands(monkeypatch):
    chain(monkeypatch, "cheap", "docling", "vlm")
    def run(answers):
        it = iter(answers)
        def fake(entry, png, langs):
            a = next(it)
            if isinstance(a, Exception):
                raise a
            return a
        return layout.detect(b"png", [], fake)
    good = answer([pic(0, 0, 10, 10, 0.9)])
    a, name, notes = run([good])
    assert name == "cheap" and notes == ["layout cheap"]
    a, name, notes = run([answer([pic(0, 0, 10, 10, 0.2)]), good])  # low confidence -> next
    assert name == "docling" and notes == ["layout cheap unsure", "layout docling (escalated from cheap)"]
    a, name, notes = run([answer([]), OSError("down"), answer([], below=None)])  # empty, then an error, then the last answer stands (even empty)
    assert name == "vlm" and a["boxes"] == [] and notes[-1] == "layout vlm (escalated from cheap, docling)"
    a, name, notes = run([answer([]), OSError(), OSError()])
    assert a is None and notes[-1] == "layout vlm failed (OSError)"
    a, name, _ = run([answer([pic(0, 0, 1, 1, 0.1)], below=None)])  # a profile without a threshold (a VLM) never escalates on score
    assert name == "cheap"
    a, name, _ = run([answer([pic(0, 0, 1, 1, 0.1)], below=0.5, score=0.9)])  # the page-level score wins over the boxes'
    assert name == "cheap"


# ---- ocr_page -----------------------------------------------------------------------------------------------------

def page(monkeypatch, tmp_path, ans, img=None, want=True, **o):
    chain(monkeypatch, "cheap")
    monkeypatch.setattr(layout, "call", lambda entry, png, langs: ans)
    return ocr.ocr_page(img or Image.new("RGB", (800, 600), "white"), o, None, str(tmp_path), "page-0003", want)


def test_crops_are_cut_at_full_resolution_and_filtered(monkeypatch, tmp_path):
    boxes = [pic(40, 30, 240, 180),  # 200x150 in the 400x300 detection frame -> 400x300 at full resolution
             pic(0, 0, 20, 20),  # too small
             pic(0, 0, 100, 100, 0.1),  # under score_min
             pic(0, 0, 400, 300, 0.9, "table"), pic(300, 200, 399, 299)]
    r = page(monkeypatch, tmp_path, answer(boxes))
    assert [f["bbox"] for f in r["figures"]] == [[80, 60, 480, 360], [600, 400, 798, 598]] and [f["member"] for f in r["figures"]] == ["page-0003/fig-1.png", "page-0003/fig-2.png"]
    assert Image.open(r["figures"][0]["file"]).size == (400, 300) and os.path.basename(r["figures"][0]["file"]) == "member-0001.png"
    assert r["tables"] == [{"file": None, "member": None, "bbox": [0, 0, 800, 600], "score": 0.9}]  # recorded, not extracted
    assert r["text"] == TEXT.strip() or r["text"] == TEXT  # a non-full profile never changes the text
    r = page(monkeypatch, tmp_path, answer(boxes), max_figures=1)
    assert len(r["figures"]) == 1


def test_gate_text_needed_and_layout_must_be_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "_tesseract", lambda img, lang=None: ("a photo", 30.0))
    monkeypatch.setattr(layout, "call", lambda *a: pytest.fail("a page with (almost) no text is the caption's job"))
    chain(monkeypatch, "cheap")
    assert ocr.ocr_page(Image.new("RGB", (800, 600)), {}, None, str(tmp_path))["figures"] == []
    monkeypatch.setattr(ocr, "_tesseract", lambda img, lang=None: (TEXT, 95.0))
    r = ocr.ocr_page(Image.new("RGB", (800, 600)), {}, None, str(tmp_path), want_layout=False)  # `layout` not asked
    assert r["figures"] == [] and r["profile"] is None


def test_full_profile_replaces_ocr_text_and_makes_a_table_member(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_URL", "http://ocr")
    monkeypatch.setenv("OCR_TOKEN", "t")
    monkeypatch.setattr(ocr, "_tesseract", lambda img, lang=None: (TEXT, 10.0))  # poor: the remote OCR would run
    monkeypatch.setattr(ocr, "remote_ocr", lambda *a, **k: pytest.fail("a full profile did the OCR"))
    boxes = [{**pic(0, 0, 400, 30, 1.0, "title"), "markdown": "# Heading"}, pic(40, 40, 240, 200), {**pic(0, 210, 400, 290, 1.0, "table"), "markdown": "| a |\n|---|\n| 1 |"},
             {**pic(0, 295, 400, 300, 1.0, "text"), "markdown": "Closing."}]
    r = page(monkeypatch, tmp_path, answer(boxes, full=True))
    assert r["full"] and r["text"] == "# Heading\n\n![](page-0003/fig-1.png)\n\n| a |\n|---|\n| 1 |\n\nClosing."
    assert r["tables"][0]["member"] == "page-0003/table-1.md" and open(r["tables"][0]["file"]).read().startswith("| a |")


# ---- the page units, end to end -------------------------------------------------------------------------------------

def make_pdf(n_pages=3) -> bytes:
    pdfium = pytest.importorskip("pypdfium2")
    pdf = pdfium.PdfDocument.new()
    for _ in range(n_pages):
        pdf.new_page(200, 100)
    b = io.BytesIO()
    pdf.save(b)
    return b.getvalue()


def run_tree(files: dict, features=(), options=None):
    d = tempfile.mkdtemp()
    for name, data in files.items():
        os.makedirs(os.path.dirname(f"{d}/in/tree/{name}"), exist_ok=True)
        open(f"{d}/in/tree/{name}", "wb").write(data)
    os.makedirs(f"{d}/work")
    s = MemSink()
    procs = processors.order([c() for c in parse.PROCESSORS] + [page_ocr.PageOcr()])
    worker.Runner(s, procs, {"files": {"ctypes": {n: "application/pdf" for n in files}}, **(options or {})}, list(features), f"{d}/work").run_tree(f"{d}/in/tree")
    shutil.rmtree(d, ignore_errors=True)
    return s


def test_scanned_pdf_pages_become_units_and_figures_members_in_page_order(monkeypatch):
    pdfium = pytest.importorskip("pypdfium2")
    texts = iter(["A text layer long enough to count.", "", ""])
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_range", lambda self, *a, **k: next(texts))
    from cadmus_parser_server import extract
    monkeypatch.setattr(extract, "file_to_markdown", lambda *a: "md")
    monkeypatch.setattr(ocr, "DPI", 144)  # a 200x100 pt page -> 400x200 px: boxes below are in this frame
    chain(monkeypatch, "cheap")
    seen = []

    def fake(entry, png, langs):
        seen.append(Image.open(io.BytesIO(png)).size)
        return answer([pic(50, 20, 250, 150)], width=400, height=200)
    monkeypatch.setattr(layout, "call", fake)
    created = []
    orig = MemSink.for_file
    monkeypatch.setattr(MemSink, "for_file", lambda self, address: (created.append(address), orig(self, address))[1])
    s = run_tree({"d.pdf": make_pdf()}, features=["layout"])
    md = s.files["parsed/d.pdf.md"].decode()
    assert md.index("<!-- page 1 -->") < md.index("<!-- page 2 (ocr) -->") < md.index("<!-- page 3 (ocr) -->") and md.count("word") == 120
    assert seen == [(400, 200)] * 2
    assert [a for a in created if len(a) == 2] == [["d.pdf", "page-0002/fig-1.png"], ["d.pdf", "page-0003/fig-1.png"]]  # page order, no page entry, no page image or unit entry
    assert not any(a[-1].startswith("page-0002.") for a in created)
    figs = json.loads(s.files["figures/d.pdf/figures.json"])
    assert [(f["page"], f["member"], f["bbox"], f["page_size"], f["profile"], f["engine"]) for f in figs] == [
        (2, "page-0002/fig-1.png", [50, 20, 250, 150], [400, 200], "cheap", "yolo-onnx"), (3, "page-0003/fig-1.png", [50, 20, 250, 150], [400, 200], "cheap", "yolo-onnx")]
    assert "original/d.pdf.d/page-0002/fig-1.png" in s.files  # `members` keeps the crop, not the page
    assert not any(k.endswith("page-0002.png") or "page-0002.json" in k for k in s.files)
    assert "d.pdf: page 2: layout cheap" in s.notes and "d.pdf: page 2 ocr tesseract (conf 95)" in s.notes


def test_no_layout_chain_or_not_asked_leaves_the_pdf_output_as_it_was(monkeypatch):
    pdfium = pytest.importorskip("pypdfium2")
    texts = iter(["A text layer long enough to count.", ""])
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_range", lambda self, *a, **k: next(texts))
    from cadmus_parser_server import extract
    monkeypatch.setattr(extract, "file_to_markdown", lambda *a: "md")
    s = run_tree({"d.pdf": make_pdf(2)})
    assert s.files["parsed/d.pdf.md"].decode().startswith("<!-- page 1 -->") and not any(k.startswith("figures/") for k in s.files)


def test_all_text_layers_keep_markitdown_output(monkeypatch):
    pdfium = pytest.importorskip("pypdfium2")
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_range", lambda self, *a, **k: "A text layer long enough to count.")
    from cadmus_parser_server import extract
    monkeypatch.setattr(extract, "file_to_markdown", lambda *a: "markitdown says")
    assert run_tree({"d.pdf": make_pdf(2)}).files["parsed/d.pdf.md"] == b"markitdown says"


def test_max_figure_members_caps_the_file_in_page_order(monkeypatch):
    pdfium = pytest.importorskip("pypdfium2")
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_range", lambda self, *a, **k: "")
    from cadmus_parser_server import extract
    monkeypatch.setattr(extract, "file_to_markdown", lambda *a: "md")
    monkeypatch.setattr(ocr, "DPI", 144)
    chain(monkeypatch, "cheap")
    monkeypatch.setattr(layout, "call", lambda e, p, l: answer([pic(50, 20, 250, 150), pic(260, 20, 390, 150)], width=400, height=200, max_figure_members=3))
    s = run_tree({"d.pdf": make_pdf(3)}, features=["layout"])
    figs = json.loads(s.files["figures/d.pdf/figures.json"])
    assert [f["member"] for f in figs if f["member"]] == ["page-0001/fig-1.png", "page-0001/fig-2.png", "page-0002/fig-1.png"]  # the first three, whatever thread ran first
    assert any("max_figure_members 3" in n for n in s.notes)
    s = run_tree({"d.pdf": make_pdf(3)}, features=["layout"], options={"page-ocr": {"max_figure_members": 1}})  # a job can only tighten it
    assert len([f for f in json.loads(s.files["figures/d.pdf/figures.json"]) if f["member"]]) == 1


def test_image_is_page_one_and_a_crop_does_not_go_through_layout_again(monkeypatch):
    chain(monkeypatch, "cheap")
    calls = []

    def fake(entry, png, langs):
        calls.append(1)
        return answer([pic(40, 30, 240, 180)])
    monkeypatch.setattr(layout, "call", fake)
    d = tempfile.mkdtemp()
    os.makedirs(f"{d}/in/tree/work", exist_ok=True)
    open(f"{d}/in/tree/p.png", "wb").write(png_bytes(800, 600))
    os.makedirs(f"{d}/work/media-decode/p.png")
    json.dump({"kind": "image"}, open(f"{d}/work/media-decode/p.png/decode.json", "w"))
    os.makedirs(f"{d}/work/media-decode/p.png.d/page-0001/fig-1.png")  # what media-decode writes for the crop in a real run: media-markdown then OCRs it too
    json.dump({"kind": "image"}, open(f"{d}/work/media-decode/p.png.d/page-0001/fig-1.png/decode.json", "w"))
    s = MemSink()
    from cadmus_parser_server import metadata
    monkeypatch.setattr(metadata, "extract_metadata", lambda *a, **k: {})
    procs = processors.order(processors.load())  # the installed chain: `members` runs before media-markdown claims the crop
    worker.Runner(s, procs, {}, ["layout"], f"{d}/work").run_tree(f"{d}/in/tree/p.png".rsplit("/", 1)[0])
    assert "parsed/p.png.d/page-0001/fig-1.png.md" in s.files  # the crop went through media-markdown (OCR with 300 characters: over the gate)
    assert len(calls) == 1  # the crop was recursed as `page-0001/fig-1.png` and media-markdown skipped layout for it
    assert "figures/p.png/figures.json" in s.files and "original/p.png.d/page-0001/fig-1.png" in s.files
    shutil.rmtree(d, ignore_errors=True)
