"""OCR of images and scanned PDF pages (docs/implemented-plans/ocr-hybrid.md): Tesseract first, the OCR sidecar when the result is poor; `ocr_page` is the one place where
the work for a page happens (OCR, the figure-extraction gate, the layout chain, crops: docs/implemented-plans/figure-extraction.md).
The wrapper puts OCR_URL and OCR_TOKEN in the environment only when the job asked for remote OCR (`features.ocr`); without them there is
no remote code path. A failure (sidecar down, timeout, token or endpoint error) never fails the parse: Tesseract's text is kept and noted."""

import io
import os
import re
import urllib.request

from cadmus_parser_server import layout

TIMEOUT = int(os.environ.get("OCR_TIMEOUT") or 120)
LANGS = os.environ.get("OCR_LANGS") or "nor+eng"  # the Tesseract packs installed in the image (native names): what a job may select from
DEFAULT_LANGS = os.environ.get("OCR_DEFAULT_LANGS") or ""  # BCP 47 tags, a subset of the available ones; a job's options["ocr"]["langs"] overrides it
MIN_CONF = int(os.environ.get("OCR_MIN_CONF") or 70)
MIN_CHARS = int(os.environ.get("OCR_MIN_CHARS") or 20)  # accepted Tesseract text below this is poor; a PDF page below it counts as scanned
DPI = int(os.environ.get("OCR_DPI") or 200)
MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES") or 50)
MAX_SIDE = int(os.environ.get("OCR_MAX_SIDE") or 2000)
TESSERACT_TIMEOUT = int(os.environ.get("OCR_TESSERACT_TIMEOUT") or 60)
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/tiff", "image/bmp"}


def enabled() -> bool:
    return bool(os.environ.get("OCR_URL") and os.environ.get("OCR_TOKEN"))


def remote_ocr(image: bytes, content_type: str = "image/png", tags: list | None = None) -> str:
    """Transcript of a PNG/JPEG (already downscaled) from the profile this job was given. Raises on any failure."""
    req = urllib.request.Request(os.environ["OCR_URL"].rstrip("/") + "/process", data=image, method="POST",
                                 headers={"Authorization": f"Bearer {os.environ['OCR_TOKEN']}", "Content-Type": content_type, **({"X-Language": ",".join(tags)} if tags else {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # a proxy variable in the environment would not be honoured here: there is none
        return r.read().decode("utf-8", "replace")  # the sidecar protocol: the transcript is the text/plain response body


# BCP 47 tag -> Tesseract pack; a tag is tried whole (`zh-Hans`), then by its primary subtag (`nb-NO` -> `nb`)
TESS = {"en": "eng", "nb": "nor", "nn": "nor", "no": "nor", "da": "dan", "sv": "swe", "fi": "fin", "is": "isl", "de": "deu", "de-Latf": "Fraktur", "fr": "fra", "es": "spa",
        "it": "ita", "pt": "por", "nl": "nld", "pl": "pol", "cs": "ces", "ru": "rus", "uk": "ukr", "el": "ell", "tr": "tur", "ar": "ara", "he": "heb", "ja": "jpn", "ko": "kor",
        "zh-Hans": "chi_sim", "zh-Hant": "chi_tra", "zh": "chi_sim"}


def resolve_langs(requested, note=lambda s: None) -> tuple[str, list]:
    """(Tesseract `lang` string, BCP 47 tags for the remote engines) from the job's `options["ocr"]["langs"]` (a list of BCP 47 tags), else OCR_DEFAULT_LANGS. A tag with no pack
    mapping or no installed pack is dropped with a note; with nothing left (or nothing asked) every installed pack is used and the remote engines get no hint."""
    asked = requested if requested else re.split(r"[\s,+]+", DEFAULT_LANGS.strip())
    asked = [t for t in asked if isinstance(t, str) and t]
    installed, packs, tags = set(LANGS.split("+")), [], []
    for t in asked:
        pack = TESS.get(t) or TESS.get(t.split("-")[0])
        if pack in installed:
            packs.append(pack)
            tags.append(t)
        else:
            note(f"ocr: language {t!r} dropped ({'no such pack installed' if pack else 'unknown language tag'})")
    return ("+".join(dict.fromkeys(packs)) or LANGS), (tags if packs else [])


def _tesseract(img, lang: str | None = None) -> tuple[str, float]:
    """(text, mean word confidence) of a PIL image."""
    import pytesseract

    d = pytesseract.image_to_data(img, lang=lang or LANGS, output_type=pytesseract.Output.DICT, timeout=TESSERACT_TIMEOUT)
    lines: dict = {}
    confs = []
    for i, w in enumerate(d["text"]):
        if w.strip() and float(d["conf"][i]) >= 0:
            lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(w)
            confs.append(float(d["conf"][i]))
    return "\n".join(" ".join(ws) for ws in lines.values()), (sum(confs) / len(confs) if confs else 0.0)


def _png(img, max_side: int = 0) -> bytes:
    img = img.copy()
    img.thumbnail((max_side or MAX_SIDE,) * 2)
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def tesseract_result(img, langs=None) -> tuple[str, float, str]:
    """(text, confidence, how) of Tesseract alone. Never raises."""
    try:
        text, conf = _tesseract(img, langs[0] if langs else None)
        return text, conf, f"tesseract (conf {conf:.0f})"
    except Exception as e:  # noqa: BLE001  no binary, timeout, bad image
        return "", 0.0, f"tesseract failed: {type(e).__name__}"


def poor_fallback(img, text: str, conf: float, how: str, langs=None) -> tuple[str, float | None, str, str]:
    """(text, confidence, engine, how): Tesseract's result, or the remote sidecar's when it is below the thresholds. Never raises."""
    if (conf >= MIN_CONF and len(text.strip()) >= MIN_CHARS) or not enabled():
        return text, conf, "tesseract", how  # without a sidecar the Tesseract result is used whatever its confidence, and the note shows it
    try:
        return remote_ocr(_png(img), tags=langs[1] if langs else None), None, "remote", "remote"
    except Exception as e:  # noqa: BLE001
        return text, conf, "tesseract", f"{how}, remote failed: {type(e).__name__}"


def ocr_engine(img, langs=None) -> tuple[str, float | None, str, str]:
    """(text, confidence, engine, how) of a PIL image: Tesseract, or the sidecar when Tesseract is below the thresholds. `engine` is "tesseract" | "remote" | "skipped";
    confidence is Tesseract's mean word confidence, None for remote. `langs` = resolve_langs(...). Never raises for OCR failures."""
    from PIL import Image

    if img.width * img.height > Image.MAX_IMAGE_PIXELS:
        return "", None, "skipped", "skipped: image too large"
    return poor_fallback(img, *tesseract_result(img, langs), langs)


PAGE_DEFAULTS = {"min_text_chars": 200, "max_side": MAX_SIDE, "score_min": 0.3, "min_side": 64, "min_area_frac": 0.01, "max_figures": 30, "workers": 4}


def _kept_pictures(boxes: list, scale: tuple, size: tuple, o: dict) -> list:
    """[(box, full-resolution bbox)] of the picture boxes worth cropping, in the answer's order."""
    out = []
    for b in boxes:
        if b.get("label") != "picture" or float(b.get("score") or 0) < float(o["score_min"]):
            continue
        x0, y0, x1, y1 = (max(0, min(lim, round(v * s))) for v, s, lim in zip(b["bbox"], (scale[0], scale[1], scale[0], scale[1]), (size[0], size[1], size[0], size[1])))
        if min(x1 - x0, y1 - y0) < int(o["min_side"]) or (x1 - x0) * (y1 - y0) < float(o["min_area_frac"]) * size[0] * size[1]:
            continue
        out.append((b, (x0, y0, x1, y1)))
    return out[:int(o["max_figures"])]


def ocr_page(img, o: dict, langs, out_dir: str, prefix: str = "page-0001", want_layout: bool = False) -> dict:
    """Everything for one page image: Tesseract (also the gate), the layout chain when `want_layout` (the job asked for `layout`) and Tesseract found at least `min_text_chars`
    (a photo without text is the caption's job), the text (the answering profile's markdown for a `full` one, else Tesseract's or the remote OCR's), and the figure crops
    (`member-NNNN.png`) and table files (`member-tNNNN.md`) in `out_dir`, cut from the full-resolution image. `prefix` names the members (`<prefix>/fig-1.png`).
    Returns {text, how, notes, full, profile, engine, max_figure_members, figures: [{file, member, bbox, score}], tables: [{file, member, bbox}]}. Never raises for OCR or layout failures."""
    from PIL import Image

    o = {**PAGE_DEFAULTS, **o}
    r = {"text": "", "how": "", "notes": [], "full": False, "profile": None, "engine": None, "max_figure_members": None, "figures": [], "tables": [], "size": [img.width, img.height]}
    if img.width * img.height > Image.MAX_IMAGE_PIXELS:
        return {**r, "how": "skipped: image too large"}
    text, conf, how = tesseract_result(img, langs)
    ans = None
    if want_layout and layout.enabled() and len(text.strip()) >= int(o["min_text_chars"]):
        ans, r["profile"], notes = layout.detect(_png(img, int(o["max_side"])), langs[1] if langs else [])
        r["notes"] += notes
    if ans is None:
        r["text"], _, r["engine"], r["how"] = poor_fallback(img, text, conf, how, langs)
        return r
    r["engine"], r["max_figure_members"], r["full"] = ans.get("engine"), ans.get("max_figure_members"), bool(ans.get("full"))
    scale = (img.width / ans["width"], img.height / ans["height"])
    os.makedirs(out_dir, exist_ok=True)
    figs, md = _kept_pictures(ans["boxes"], scale, img.size, o), []
    for k, (b, bb) in enumerate(figs, 1):
        path = os.path.join(out_dir, f"member-{k:04d}.png")
        crop = img.crop(bb)
        crop.convert("RGBA" if crop.mode in ("RGBA", "LA", "P") else "RGB").save(path, "PNG")
        r["figures"].append({"file": path, "member": f"{prefix}/fig-{k}.png", "bbox": list(bb), "score": b.get("score"), "link": f"![]({prefix}/fig-{k}.png)"})
    links = {id(b): f["link"] for (b, _), f in zip(figs, r["figures"])}
    for b in ans["boxes"]:
        if b.get("label") == "table":  # recorded always; a member only when the answering profile gave its markdown (`full`)
            j, bb = len(r["tables"]) + 1, [round(v * s) for v, s in zip(b["bbox"], (scale[0], scale[1], scale[0], scale[1]))]
            t = {"file": None, "member": None, "bbox": bb, "score": b.get("score")}
            if r["full"] and b.get("markdown"):
                t["file"], t["member"] = os.path.join(out_dir, f"member-t{j:04d}.md"), f"{prefix}/table-{j}.md"
                with open(t["file"], "w") as f:
                    f.write(b["markdown"])
            r["tables"].append(t)
        if r["full"] and (m := links.get(id(b)) or b.get("markdown")):
            md.append(m)
    if r["full"] and md:  # the answering profile did the OCR: Tesseract's text (and the remote OCR) is not used for this page
        r["text"], r["how"] = "\n\n".join(md), f"layout {r['profile']} (full)"
    else:
        r["text"], _, _, r["how"] = poor_fallback(img, text, conf, how, langs)
    return r
