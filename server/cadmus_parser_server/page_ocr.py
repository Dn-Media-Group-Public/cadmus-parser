"""Scanned pages as page units (docs/implemented-plans/figure-extraction.md). The parent (`documents` for a PDF, `media-markdown` for an image) writes one unit manifest `page-NNNN.json`
per page ({page, image, prefix, layout}) and runs the chain on it as a synthetic member, the idiom of caption-split; `page-ocr` accepts only those, claims them and leaves its
result (`page.json`: text, how, notes, figures, tables) and the crops in its `ctx.dir()`. It creates no member and no output: the parent assembles the text, and the figure crops
become real members of the real file, `<pdf>.d/page-0003/fig-1.png`, recursed by the parent in page order. No page image is ever an output.
Options `options["page-ocr"]`: min_text_chars (200), max_side (2000), score_min (0.3), min_side (64), min_area_frac (0.01), max_figures (30) per page, max_figure_members per file
(tightens the answering profile's own cap), workers (4)."""

import json
import os
import re

from cadmus_parser_server import layout, ocr
from cadmus_parser_server.asr import HANDLERS

UNIT_NAME = re.compile(r"^page-\d{4,}\.json$")
FIGURE_NAME = re.compile(r"^page-\d{4,}/(fig-\d+\.png|table-\d+\.md)$")  # what the parent made from a page: it never goes through layout again


def is_figure(file) -> bool:
    return bool(file.depth) and bool(FIGURE_NAME.match(file.name))


def _unit_of(file) -> dict | None:
    if file.index_address is None or not UNIT_NAME.match(file.name):
        return None
    try:
        with open(file.path) as f:
            u = json.load(f)
        return u if u["image"] and u["prefix"] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


class PageOcr:
    name, top_dirs, before = "page-ocr", (), HANDLERS

    def accepts(self, file) -> bool:
        return _unit_of(file) is not None

    def process(self, sink, file, ctx) -> None:
        ctx.claim()  # first: a page that fails is left out by its parent, never parsed as a file of its own
        from PIL import Image

        u = _unit_of(file)
        with Image.open(u["image"]) as img:
            img.load()
            res = ocr.ocr_page(img, ctx.options_of("page-ocr"), ocr.resolve_langs(ctx.options_of("ocr").get("langs")), ctx.dir(), u["prefix"], u["layout"])
        with open(os.path.join(ctx.dir(), "page.json"), "w") as f:
            json.dump(res, f)


# ---- the parent's side --------------------------------------------------------------------------------------------

def run_pages(ctx, sink, file, pages: list, layout_ok: bool) -> list:
    """pages [(number, image path)] -> [page result or None], in order: the units are run in parallel as synthetic members of `file`."""
    o = {**ocr.PAGE_DEFAULTS, **ctx.options_of("page-ocr")}
    ocr.resolve_langs(ctx.options_of("ocr").get("langs"), sink.note)  # the notes about dropped languages, once
    base, items = ctx.dir(), []
    for n, image in pages:
        name = f"page-{n:04d}.json"
        with open(os.path.join(base, name), "w") as f:
            json.dump({"page": n, "image": image, "prefix": f"page-{n:04d}", "layout": layout_ok}, f)
        items.append((os.path.join(base, name), name))
    out = []
    for m in ctx.recurse_many(items, workers=int(o["workers"]), synthetic=True):
        try:
            with open(os.path.join(m.dir(m.claimed_by), "page.json")) as f:
                out.append(json.load(f))
        except (OSError, ValueError, TypeError):
            out.append(None)
    return out


def emit_figures(ctx, sink, file, pages: list, results: list) -> None:
    """The crops of every page as members of `file` (in page order, so the entries in index.json are in page order), and `figures/<rel>/figures.json`. A cap on members per
    file (the answering profile's `max_figure_members`, tightened by the job's) stops the extraction with a note; a table without markdown is recorded but not extracted."""
    o, records, count, stopped = ctx.options_of("page-ocr"), [], 0, False
    for (n, _), res in zip(pages, results):
        if not res:
            continue
        for note in res["notes"]:
            sink.note(f"{file.rel}: page {n}: {note}")
        caps = [int(c) for c in (res.get("max_figure_members"), o.get("max_figure_members")) if c]
        cap = min(caps) if caps else None
        for label, items in (("picture", res["figures"]), ("table", res["tables"])):
            for it in items:
                rec = {"page": n, "label": label, "member": None, "bbox": it["bbox"], "page_size": res["size"], "score": it.get("score"), "profile": res["profile"], "engine": res["engine"]}
                if it["file"] and cap is not None and count >= cap:
                    if not stopped:
                        sink.note(f"{file.rel}: figures stopped at page {n} (max_figure_members {cap})")
                    stopped = True
                elif it["file"]:
                    ctx.recurse(it["file"], it["member"])
                    rec["member"], count = it["member"], count + 1
                records.append(rec)
    if records:
        sink.put_bytes(f"figures/{file.rel}/figures.json", json.dumps(records).encode(), "application/json")


def want_layout(ctx, file) -> bool:
    return "layout" in ctx.features and layout.enabled() and not is_figure(file)


def pdf_markdown(raw: bytes, path: str, text: str, sink, ctx, file) -> str:
    """`text` is markitdown's output for the PDF; returns what parsed/<path>.md should hold: the same unless a page lacks a text layer, then the document is rebuilt per page
    (pdfium's text, and OCR sections for the scanned pages, which are page units; figures become members of the PDF)."""
    import pypdfium2

    pdf = pypdfium2.PdfDocument(raw)
    pages = [p.get_textpage().get_text_range() for p in pdf]
    if all(len(t.strip()) >= ocr.MIN_CHARS for t in pages):
        return text  # a text layer everywhere: markitdown's output stands
    # ponytail: with any scanned page the document is rebuilt from pdfium's per-page text (markitdown's formatting is lost); fine for scans
    scanned, last = [], len(pages)
    for n, t in enumerate(pages, 1):
        if len(t.strip()) < ocr.MIN_CHARS:
            if len(scanned) >= ocr.MAX_PAGES:
                sink.note(f"{path}: ocr stopped at page {n - 1}")
                last = n - 1
                break
            scanned.append(n)
    base, units = ctx.dir(), []
    for n in scanned:
        image = os.path.join(base, f"page-{n:04d}.png")
        pdf[n - 1].render(scale=ocr.DPI / 72).to_pil().save(image, "PNG")
        units.append((n, image))
    results = dict(zip(scanned, run_pages(ctx, sink, file, units, want_layout(ctx, file)))) if units else {}
    out = []
    for n in range(1, last + 1):
        if n not in results:
            out.append(f"\n\n<!-- page {n} -->\n{pages[n - 1]}")
            continue
        res = results[n]
        if res is None:
            sink.note(f"{path}: page {n} ocr failed")
        else:
            sink.note(f"{path}: page {n} ocr {res['how']}")
        out.append(f"\n\n<!-- page {n} (ocr) -->\n{res['text'] if res else ''}")
    emit_figures(ctx, sink, file, units, [results[n] for n, _ in units])
    return "".join(out).strip()


def image_markdown(file, ctx, sink) -> str:
    """The OCR text of an image file, as page 1 (media-markdown); its figures become members `page-0001/fig-1.png`."""
    units = [(1, file.path)]
    (res,) = run_pages(ctx, sink, file, units, want_layout(ctx, file))
    if res is None:
        sink.note(f"{file.rel}: ocr failed")
        return ""
    sink.note(f"{file.rel}: ocr {res['how']}")
    emit_figures(ctx, sink, file, units, [res])
    return res["text"]


def plain_image_text(raw: bytes, path: str, ctx, sink) -> str:
    """OCR of an image that media-decode did not take (`documents`): the text only, no layout. Never fails the parse."""
    import io

    from PIL import Image

    try:
        langs = ocr.resolve_langs(ctx.options_of("ocr").get("langs"), sink.note)
        text, _, _, how = ocr.ocr_engine(Image.open(io.BytesIO(raw)), langs)
        sink.note(f"{path}: ocr {how}")
        return text
    except ImportError as e:
        sink.note(f"{path}: ocr unavailable ({e})")
    except Exception as e:  # noqa: BLE001  OCR never fails the parse
        sink.note(f"{path}: ocr failed ({type(e).__name__}: {e})")
    return ""
