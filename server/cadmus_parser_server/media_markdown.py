"""`media-markdown` (docs/implemented-plans/media-markdown.md): one time-interleaved `parsed/<rel>.md` (+ `terse/`) for an image, video or audio file that media-decode decoded,
built from whichever of asr / caption / frame-ocr produced a result (their scratch copies in `ctx.work/<processor>/<rel>/`). It claims the file, so `documents` never sees
media; an image's OCR (`ocr.maybe_ocr`) runs here. Options `options["media-markdown"]`: window_seconds (30: window length when there are no caption windows)."""

import json
import os

from cadmus_parser_server import ocr
from cadmus_parser_server.asr import HANDLERS
from cadmus_parser_server.parse import _ctype, _terse

DEFAULTS = {"window_seconds": 30}


def _ts(t: float) -> str:
    t = int(t)
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:d}:{t % 60:02d}"


def build_markdown(kind: str, captions: list, ocr_entries: list, segments: list, window_seconds: float = 30, image_text: str = "") -> str:
    """captions [{t_start, t_end, text, outcome}], ocr_entries [{t_start, text}], segments [{offset, text, speaker?}]; "" when there is nothing to say."""
    caps = [c for c in captions if c.get("outcome", "ok") == "ok" and c.get("text", "").strip()]
    if kind == "image":
        return "\n\n".join(x for x in [*(f"**Scene:** {c['text'].strip()}" for c in caps), image_text.strip()] if x)
    ocr_entries, segments = [e for e in ocr_entries if e["text"].strip()], [s for s in segments if s["text"].strip()]
    bounds = sorted({c["t_start"] for c in captions})  # every caption unit, even a left-out one, sets a window
    end = max([0, *(e["t_start"] for e in ocr_entries), *(s["offset"] for s in segments), *(c["t_start"] for c in captions)])
    if not bounds:
        bounds = [i * window_seconds for i in range(int(end // window_seconds) + 1)]
    wins = {b: {"cap": [], "ocr": [], "sp": []} for b in bounds}

    def win(t):  # last boundary <= t (the first one for anything earlier)
        return max((b for b in bounds if b <= t), default=bounds[0])

    for c in caps:
        wins[win(c["t_start"])]["cap"].append(c["text"].strip())
    for e in ocr_entries:
        wins[win(e["t_start"])]["ocr"].append(e["text"].strip())
    for s in segments:
        wins[win(s["offset"])]["sp"].append((f"{s['speaker']}: " if s.get("speaker") else "") + s["text"].strip())
    out = []
    for b in bounds:
        w = wins[b]
        parts = [f"**Scene:** {' '.join(w['cap'])}" if w["cap"] else "", *(f"**On screen:** {t}" for t in w["ocr"]), f"**Speech:** {' '.join(w['sp'])}" if w["sp"] else ""]
        if any(parts):
            out.append(f"## {_ts(b)}\n\n" + "\n\n".join(p for p in parts if p))
    return "\n\n".join(out)


def _load(ctx, proc: str, rel: str, name: str):
    try:
        with open(os.path.join(ctx.work, proc, rel, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class MediaMarkdown:
    name, top_dirs, after, before = "media-markdown", ("parsed", "terse"), ("media-decode", "asr-split", "caption-split", "frame-ocr"), HANDLERS

    def accepts(self, file) -> bool:
        return file.index_address is None

    def process(self, sink, file, ctx) -> None:
        d = _load(ctx, "media-decode", file.rel, "decode.json")
        kind = (d or {}).get("kind")
        if kind not in ("image", "video", "audio"):
            return
        ctx.claim()
        o = {**DEFAULTS, **ctx.options_of("media-markdown")}
        caps = (_load(ctx, "caption-split", file.rel, "captions.json") or {}).get("captions") or []
        ents = _load(ctx, "frame-ocr", file.rel, "ocr.json") or []
        segs = (_load(ctx, "asr-split", file.rel, "transcript.json") or {}).get("segments") or []
        text = ""
        if kind == "image":
            with open(file.path, "rb") as f:
                text = ocr.maybe_ocr(f.read(), file.rel, _ctype(file, ctx), "", sink.note)
        md = build_markdown(kind, caps, ents, segs, float(o["window_seconds"]), text)
        if not md.strip():
            return
        sink.put_bytes(f"parsed/{file.rel}.md", md.encode(), "text/markdown")
        _terse(sink, f"{file.rel}.md", md)
