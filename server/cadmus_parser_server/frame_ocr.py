"""`frame-ocr` (docs/plans/06-frame-ocr.md): OCR of the frames of a video's frame store (media-decode) with the `ocr` engine, timestamped and deduplicated across time, written to
`frame-ocr/<file.rel>/ocr.json`. Runs only when the job asked for the `ocr` feature. Options `options["frame-ocr"]`: pass (a frame pass file stem such as `full-0.5`; default: the
highest-resolution pass with fps <= 1, else the highest-resolution one), change_threshold (0.02: mean grey difference, 0..1, below which a frame counts as unchanged),
max_frames (5000: OCR runs per video; a note says when it was reached)."""

import json
import os
import subprocess
from difflib import SequenceMatcher
from functools import partial

from cadmus_parser_server import ocr
from cadmus_parser_server.asr import HANDLERS
from cadmus_parser_server.media_decode import _probe, facts

DEFAULTS = {"pass": "", "change_threshold": 0.02, "max_frames": 5000}
SAME_TEXT = 0.9  # SequenceMatcher ratio at or above which two consecutive OCR results are one entry
THUMB = (32, 18)


# ---- pure helpers -------------------------------------------------------------------------------------------------

def pick_pass(passes: list, name: str = "") -> dict | None:
    """passes [{file, fps, width, height}]: the one named `name` (file stem or path), else the largest with fps <= 1, else the largest."""
    if name:
        return next((p for p in passes if name in (p["file"], os.path.splitext(os.path.basename(p["file"]))[0])), None)
    big = lambda ps: max(ps, key=lambda p: (p["width"] or 0) * (p["height"] or 0), default=None)  # noqa: E731
    return big([p for p in passes if 0 < p["fps"] <= 1]) or big(passes)


def thumb_diff(a: bytes, b: bytes) -> float:
    """Mean absolute difference (0..1) of two equally long 8-bit grey thumbnails."""
    return sum(abs(x - y) for x, y in zip(a, b)) / (255 * max(len(a), 1))


def ocr_frames(frames, ocr_fn, step: float, threshold: float, max_frames: int) -> tuple[list, bool]:
    """frames: iterable of (t, thumb bytes, image factory); ocr_fn(image) -> (text, conf, engine). A frame that differs from the last OCRed one by less than `threshold`
    extends the current entry; consecutive near-identical texts merge; an empty text closes the entry. Returns ([{t_start, t_end, text, conf, engine}], capped)."""
    out, cur, last, runs = [], None, None, 0
    for t, thumb, image in frames:
        if last is not None and thumb_diff(last, thumb) < threshold:
            if cur:
                cur["t_end"] = t + step
            continue
        if runs >= max_frames:
            return out, True
        last, runs = thumb, runs + 1
        text, conf, engine = ocr_fn(image())
        if not text.strip():
            cur = None
        elif cur and SequenceMatcher(None, cur["text"], text).ratio() >= SAME_TEXT:
            cur["t_end"] = t + step
            if conf is not None and (cur["conf"] is None or conf > cur["conf"]):  # keep the better reading
                cur.update(text=text, conf=conf, engine=engine)
        else:
            cur = {"t_start": t, "t_end": t + step, "text": text, "conf": None if conf is None else round(conf), "engine": engine}
            out.append(cur)
    return out, False


def read_frames(path: str, width: int, height: int, fps: float):
    """Yield (t, thumb bytes, image factory) of every frame of a frame-store file: ffmpeg decodes sequentially to raw 8-bit grey (kept at the stored resolution)."""
    from PIL import Image

    n = width * height
    p = subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file", "-i", path, "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        i = 0
        while len(buf := p.stdout.read(n)) == n:
            img = Image.frombytes("L", (width, height), buf)
            yield i / fps, img.resize(THUMB, Image.BOX).tobytes(), lambda img=img: img
            i += 1
    finally:
        p.kill()
        p.wait()


def _engine(img, langs=None):
    text, conf, engine, _ = ocr.ocr_engine(img, langs)
    return text, conf, engine


class FrameOcr:
    name, top_dirs, after, before = "frame-ocr", ("frame-ocr",), ("media-decode",), HANDLERS

    def accepts(self, file) -> bool:
        return bool(os.environ.get("OCR_URL") and os.environ.get("OCR_TOKEN")) and file.index_address is None

    def process(self, sink, file, ctx) -> None:
        if "ocr" not in ctx.features:
            return
        dec = os.path.join(ctx.work, "media-decode", file.rel)
        try:
            with open(os.path.join(dec, "decode.json")) as f:
                d = json.load(f)
        except OSError:
            return
        if d.get("kind") != "video":
            return
        o = {**DEFAULTS, **ctx.options_of("frame-ocr")}
        passes = []
        for fp in d.get("frame_passes") or []:
            try:
                passes.append({"file": fp["file"], **{k: facts(_probe(os.path.join(dec, fp["file"])))[k] for k in ("fps", "width", "height")}})
            except Exception:  # noqa: BLE001  a missing or unreadable pass file is not a candidate
                pass
        p = pick_pass(passes, str(o["pass"]))
        if not p or p["fps"] <= 0 or not p["width"]:
            sink.note(f"frame-ocr: {file.rel}: no usable frame pass" + (f" {o['pass']!r}" if o["pass"] else ""))
            return
        entries, capped = ocr_frames(read_frames(os.path.join(dec, p["file"]), p["width"], p["height"], p["fps"]), partial(_engine, langs=ocr.resolve_langs(ctx.options_of("ocr").get("langs"), sink.note)), 1 / p["fps"],
                                     float(o["change_threshold"]), int(o["max_frames"]))
        if capped:
            sink.note(f"frame-ocr: {file.rel}: stopped after {o['max_frames']} OCR runs at {entries[-1]['t_end'] if entries else 0:.0f}s")
        sink.put_bytes(f"frame-ocr/{file.rel}/ocr.json", json.dumps(entries).encode(), "application/json")
        with open(os.path.join(ctx.dir(), "ocr.json"), "w") as f:  # scratch copy for media-markdown
            json.dump(entries, f)
