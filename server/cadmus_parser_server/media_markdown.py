"""`media-markdown` (docs/implemented-plans/media-markdown.md): one time-ordered `parsed/<rel>.md` for an image, video or audio file that media-decode decoded,
built from whichever of asr / caption / frame-ocr produced a result (their scratch copies in `ctx.work/<processor>/<rel>/`). It claims the file, so `documents` never sees
media; an image's OCR (`ocr.maybe_ocr`) runs here."""

import json
import os

from cadmus_parser_server import ocr
from cadmus_parser_server.asr import HANDLERS
from cadmus_parser_server.parse import _ctype


def _ts(t: float) -> str:
    t = int(t)
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:d}:{t % 60:02d}"


def build_markdown(kind: str, captions: list, ocr_entries: list, segments: list, image_text: str = "") -> str:
    """captions [{t_start, text, outcome}], ocr_entries [{t_start, text}], segments [{offset, text, speaker?}] -> one paragraph per item in time order:
    `` `0:06` **Speaker**: text ``, `` `0:08` _SCENE_: text ``, `` `0:05` _ON SCREEN_: text ``. An image has no times. "" when there is nothing to say."""
    one = lambda t: " / ".join(x.strip() for x in t.splitlines() if x.strip())  # noqa: E731  OCR text is multi-line: one paragraph
    items = [(c["t_start"], 0, "_SCENE_: " + one(c["text"])) for c in captions if c.get("outcome", "ok") == "ok" and c.get("text", "").strip()]
    items += [(e["t_start"], 1, "_ON SCREEN_: " + one(e["text"])) for e in ocr_entries if e["text"].strip()]
    items += [(s["offset"], 2, (f"**{s['speaker']}**: " if s.get("speaker") else "") + one(s["text"])) for s in segments if s["text"].strip()]
    if kind == "image":
        return "\n\n".join([*(t for _, _, t in sorted(items)), *([f"_ON SCREEN_: {one(image_text)}"] if image_text.strip() else [])])
    return "\n\n".join(f"`{_ts(t)}` {text}" for t, _, text in sorted(items))


def _load(ctx, proc: str, rel: str, name: str):
    try:
        with open(os.path.join(ctx.work, proc, rel, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class MediaMarkdown:
    name, top_dirs, after, before = "media-markdown", ("parsed",), ("media-decode", "asr-split", "caption-split", "frame-ocr"), HANDLERS

    def accepts(self, file) -> bool:
        return file.index_address is None

    def process(self, sink, file, ctx) -> None:
        d = _load(ctx, "media-decode", file.rel, "decode.json")
        kind = (d or {}).get("kind")
        if kind not in ("image", "video", "audio"):
            return
        ctx.claim()
        caps = (_load(ctx, "caption-split", file.rel, "captions.json") or {}).get("captions") or []
        ents = _load(ctx, "frame-ocr", file.rel, "ocr.json") or []
        segs = (_load(ctx, "asr-split", file.rel, "transcript.json") or {}).get("segments") or []
        text = ""
        if kind == "image":
            with open(file.path, "rb") as f:
                text = ocr.maybe_ocr(f.read(), file.rel, _ctype(file, ctx), "", sink.note)
        md = build_markdown(kind, caps, ents, segs, text)
        if not md.strip():
            return
        sink.put_bytes(f"parsed/{file.rel}.md", md.encode(), "text/markdown")  # no terse/: it keeps headings and links only, here bare timestamps
