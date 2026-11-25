"""OCR of images and scanned PDF pages (docs/implemented-plans/ocr-hybrid.md): Tesseract first, the OCR sidecar when the result is poor.
The wrapper puts OCR_URL and OCR_TOKEN in the environment only when the job asked for remote OCR (`features.ocr`); without them there is
no remote code path. A failure (sidecar down, timeout, token or endpoint error) never fails the parse: Tesseract's text is kept and noted."""

import io
import os
import urllib.request

TIMEOUT = int(os.environ.get("OCR_TIMEOUT") or 120)
LANGS = os.environ.get("OCR_LANGS") or "nor+eng"
MIN_CONF = int(os.environ.get("OCR_MIN_CONF") or 70)
MIN_CHARS = int(os.environ.get("OCR_MIN_CHARS") or 20)  # accepted Tesseract text below this is poor; a PDF page below it counts as scanned
DPI = int(os.environ.get("OCR_DPI") or 200)
MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES") or 50)
MAX_SIDE = int(os.environ.get("OCR_MAX_SIDE") or 2000)
TESSERACT_TIMEOUT = int(os.environ.get("OCR_TESSERACT_TIMEOUT") or 60)
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/tiff", "image/bmp"}


def enabled() -> bool:
    return bool(os.environ.get("OCR_URL") and os.environ.get("OCR_TOKEN"))


def remote_ocr(image: bytes, content_type: str = "image/png") -> str:
    """Transcript of a PNG/JPEG (already downscaled) from the profile this job was given. Raises on any failure."""
    req = urllib.request.Request(os.environ["OCR_URL"].rstrip("/") + "/process", data=image, method="POST",
                                 headers={"Authorization": f"Bearer {os.environ['OCR_TOKEN']}", "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # a proxy variable in the environment would not be honoured here: there is none
        return r.read().decode("utf-8", "replace")  # the sidecar protocol: the transcript is the text/plain response body


def _tesseract(img) -> tuple[str, float]:
    """(text, mean word confidence) of a PIL image."""
    import pytesseract

    d = pytesseract.image_to_data(img, lang=LANGS, output_type=pytesseract.Output.DICT, timeout=TESSERACT_TIMEOUT)
    lines: dict = {}
    confs = []
    for i, w in enumerate(d["text"]):
        if w.strip() and float(d["conf"][i]) >= 0:
            lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(w)
            confs.append(float(d["conf"][i]))
    return "\n".join(" ".join(ws) for ws in lines.values()), (sum(confs) / len(confs) if confs else 0.0)


def _png(img) -> bytes:
    img = img.copy()
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def ocr_engine(img) -> tuple[str, float | None, str, str]:
    """(text, confidence, engine, how) of a PIL image: Tesseract, or the sidecar when Tesseract is below the thresholds. `engine` is "tesseract" | "remote" | "skipped";
    confidence is Tesseract's mean word confidence, None for remote. Never raises for OCR failures."""
    from PIL import Image

    if img.width * img.height > Image.MAX_IMAGE_PIXELS:
        return "", None, "skipped", "skipped: image too large"
    try:
        text, conf = _tesseract(img)
        how = f"tesseract (conf {conf:.0f})"
    except Exception as e:  # noqa: BLE001  no binary, timeout, bad image
        text, conf, how = "", 0.0, f"tesseract failed: {type(e).__name__}"
    if (conf >= MIN_CONF and len(text.strip()) >= MIN_CHARS) or not enabled():
        return text, conf, "tesseract", how  # without a sidecar the Tesseract result is used whatever its confidence, and the note shows it
    try:
        return remote_ocr(_png(img)), None, "remote", "remote"
    except Exception as e:  # noqa: BLE001
        return text, conf, "tesseract", f"{how}, remote failed: {type(e).__name__}"


def ocr_image(img) -> tuple[str, str]:
    """(text, how) of a PIL image, see ocr_engine."""
    text, _, _, how = ocr_engine(img)
    return text, how


def maybe_ocr(raw: bytes, path: str, ctype: str, text: str, note) -> str:
    """`text` is markitdown's output for `raw`; returns the text parsed/<path>.md should hold. No-op unless an image or a PDF with
    pages lacking a text layer. `note(str)` records provenance."""
    low = path.lower()
    try:
        if ctype in IMAGE_TYPES:
            from PIL import Image

            out, how = ocr_image(Image.open(io.BytesIO(raw)))
            note(f"{path}: ocr {how}")
            return out
        if ctype == "application/pdf" or low.endswith(".pdf"):
            return _pdf(raw, path, text, note)
    except ImportError as e:
        note(f"{path}: ocr unavailable ({e})")
    except Exception as e:  # noqa: BLE001  OCR never fails the parse
        note(f"{path}: ocr failed ({type(e).__name__}: {e})")
    return text


def _pdf(raw: bytes, path: str, text: str, note) -> str:
    import pypdfium2

    pdf = pypdfium2.PdfDocument(raw)
    pages = [p.get_textpage().get_text_range() for p in pdf]
    if all(len(t.strip()) >= MIN_CHARS for t in pages):
        return text  # a text layer everywhere: markitdown's output stands
    # ponytail: with any scanned page the document is rebuilt from pdfium's per-page text (markitdown's formatting is lost); fine for scans
    out, done = [], 0
    for n, (page, t) in enumerate(zip(pdf, pages), 1):
        if len(t.strip()) >= MIN_CHARS:
            out.append(f"\n\n<!-- page {n} -->\n{t}")
            continue
        if done >= MAX_PAGES:
            note(f"{path}: ocr stopped at page {n - 1}")
            break
        done += 1
        o, how = ocr_image(page.render(scale=DPI / 72).to_pil())
        note(f"{path}: page {n} ocr {how}")
        out.append(f"\n\n<!-- page {n} (ocr) -->\n{o}")
    return "".join(out).strip()
