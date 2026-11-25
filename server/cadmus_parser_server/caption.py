"""Caption (docs/plans/05-caption.md): `caption-split` turns an image, or each window of up to 20 keyframes of a video, into a unit FILE (a JSON manifest) and
runs the processor chain on it as a synthetic member; `caption-primary`, `caption-fallback` and `caption-skip` (cadmus's own priority order) caption one unit each
through the caption sidecar; `caption-split` then writes `caption/<file.rel>/captions.json`, the only output. A unit's result is a scratch file in the claiming
processor's `ctx.dir()` (`caption.json`, or `attempt.json` for a failed attempt). Options: `options["caption"]` (shared by all four): max_side (1536), window_frames (20),
workers (4). Env: CAPTION_URL/CAPTION_TOKEN (job asked for `caption`), CAPTION_FALLBACK_URL/CAPTION_FALLBACK_TOKEN (`caption-fallback`)."""

import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

from cadmus_parser_server import modelcheck
from cadmus_parser_server.asr import HANDLERS

DEFAULTS = {"max_side": 1536, "window_frames": 20, "workers": 4}
MAX_FRAMES = 20  # the sidecar's limit
TIMEOUT = int(os.environ.get("CAPTION_TIMEOUT") or 120)
BUSY_RETRIES, BUSY_WAIT = 5, float(os.environ.get("CAPTION_BUSY_WAIT") or 3)  # 503 = the sidecar is full: wait, not an attempt
UNIT_NAME = re.compile(r"^window-\d{4,}\.json$")


# ---- pure helpers -------------------------------------------------------------------------------------------------

def plan_windows(keyframes: list, duration: float, size: int) -> list:
    """keyframes [{n, t}] -> [{t_start, t_end, frames: [n], times: [t]}]: `size` consecutive keyframes per window; a window ends where the next begins (the last at `duration`)."""
    size = max(1, min(int(size), MAX_FRAMES))
    chunks = [keyframes[i:i + size] for i in range(0, len(keyframes), size)]
    return [{"t_start": c[0]["t"], "t_end": max(chunks[i + 1][0]["t"] if i + 1 < len(chunks) else duration, c[-1]["t"]), "frames": [k["n"] for k in c], "times": [k["t"] for k in c]}
            for i, c in enumerate(chunks)]


def scaled_jpeg(path: str, max_side: int) -> bytes:
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=85)
        return out.getvalue()


def multipart(parts: list) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    body = b"".join(f"--{b}\r\nContent-Type: image/jpeg\r\n\r\n".encode() + p + b"\r\n" for p in parts) + f"--{b}--\r\n".encode()
    return body, f"multipart/mixed; boundary={b}"


# ---- the sidecar call ---------------------------------------------------------------------------------------------

def call(prefix: str, unit: dict, max_side: int) -> dict:
    """One POST /process to the sidecar of `<prefix>_URL`. Raises on any failure; waits (not an attempt) while the sidecar answers 503."""
    jpegs = [scaled_jpeg(p, max_side) for p in unit["paths"]]
    headers = {"Authorization": f"Bearer {os.environ[prefix + '_TOKEN']}"}
    if unit["kind"] == "image":
        body, headers["Content-Type"] = jpegs[0], "image/jpeg"
    else:
        body, headers["Content-Type"] = multipart(jpegs)
        headers["X-Frame-Times"] = ",".join(f"{t:.2f}" for t in unit["times"])
    for i in range(BUSY_RETRIES + 1):
        req = urllib.request.Request(os.environ[prefix + "_URL"].rstrip("/") + "/process", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 503 or i == BUSY_RETRIES:
                raise
            time.sleep(BUSY_WAIT * (i + 1))
    raise RuntimeError("unreachable")


def _unit_of(file) -> dict | None:
    """The manifest of a unit file made by caption-split, else None (an archive member that merely has such a name is not synthetic)."""
    if file.index_address is None or not UNIT_NAME.match(file.name):
        return None
    try:
        with open(file.path) as f:
            u = json.load(f)
        return u if u["kind"] in ("image", "video") and u["paths"] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write(ctx, name: str, rec: dict) -> None:
    with open(os.path.join(ctx.dir(), name), "w") as f:
        json.dump(rec, f)


class _Unit:
    """Shared by the two model processors: accept only unit files made by caption-split."""
    top_dirs, before = ("caption",), HANDLERS
    max_attempts = 2

    def accepts(self, file) -> bool:
        return bool(os.environ.get(self.env + "_URL") and os.environ.get(self.env + "_TOKEN")) and _unit_of(file) is not None

    def process(self, sink, file, ctx) -> None:
        unit, statuses, reasons, model, res = _unit_of(file), [], [], "", None
        side = int({**DEFAULTS, **ctx.options_of("caption")}["max_side"])
        for _ in range(self.max_attempts):
            try:
                resp = call(self.env, unit, side)
                model = resp.get("model") or model
                res = modelcheck.evaluate_text(resp)
            except Exception as e:  # noqa: BLE001  an unreachable sidecar or a bad answer is a failed attempt
                res = modelcheck.Result("invalid", reason=f"call failed ({type(e).__name__})")
            statuses.append(res.status)
            if res.reason:
                reasons.append(res.reason)
            if res.status in ("ok", "blocked"):  # a block is deterministic for the same input: retrying on the same model never helps
                break
        rec = {"model": model, "attempts": len(statuses), "statuses": statuses, "reasons": reasons, "role": self.role}
        if res.status == "ok":
            _write(ctx, "caption.json", {**rec, "text": res.text, "outcome": "ok"})
            ctx.claim()
        else:
            _write(ctx, "attempt.json", rec)
            sink.note(f"{self.name}: {file.name} failed ({'; '.join(reasons) or 'no result'})")


class CaptionPrimary(_Unit):
    name, role, env = "caption-primary", "primary", "CAPTION"


class CaptionFallback(_Unit):
    name, role, env, after = "caption-fallback", "fallback", "CAPTION_FALLBACK", ("caption-primary",)


class CaptionSkip:
    """A unit nobody captioned: a result without text, `blocked` if every attempt was blocked, else `invalid`. Always claims; the parent notes it."""
    name, top_dirs, after, before = "caption-skip", ("caption",), ("caption-fallback", "caption-primary"), HANDLERS

    def accepts(self, file) -> bool:
        return _unit_of(file) is not None

    def process(self, sink, file, ctx) -> None:
        recs = []
        for proc in ("caption-primary", "caption-fallback"):
            p = os.path.join(ctx.work, proc, file.rel, "attempt.json")
            if os.path.exists(p):
                with open(p) as f:
                    recs.append(json.load(f))
        statuses = [s for r in recs for s in r["statuses"]]
        _write(ctx, "caption.json", {"text": "", "outcome": "blocked" if statuses and all(s == "blocked" for s in statuses) else "invalid", "role": "none",
                                     "model": next((r["model"] for r in recs if r["model"]), ""), "attempts": sum(r["attempts"] for r in recs),
                                     "reasons": [x for r in recs for x in r["reasons"]]})
        ctx.claim()


class CaptionSplit:
    """The parent: makes the units, recurses on each (in parallel), writes `captions.json`. Runs only for an image or video decoded by media-decode."""
    name, top_dirs, after, before = "caption-split", ("caption",), ("media-decode",), HANDLERS

    def accepts(self, file) -> bool:
        return bool(os.environ.get("CAPTION_URL") and os.environ.get("CAPTION_TOKEN")) and file.index_address is None

    def units(self, file, ctx, o) -> tuple[str, list]:
        dec = os.path.join(ctx.work, "media-decode", file.rel)
        try:
            with open(os.path.join(dec, "decode.json")) as f:
                d = json.load(f)
        except OSError:
            return "", []  # media-decode did not see it
        if d.get("kind") == "image":
            web = os.path.join(dec, "web.png")  # a format a browser (and a vision model) may not open: media-decode converted it
            path = web if (d.get("renditions") or {}).get("web") == "web.png" and os.path.exists(web) else file.path
            return "image", [{"t_start": 0.0, "t_end": 0.0, "frames": [0], "times": [0.0], "paths": [path]}]
        if d.get("kind") == "video":
            with open(os.path.join(dec, "keyframes.json")) as f:
                kf = json.load(f)
            ws = plan_windows(kf, float(d.get("duration") or 0), int(o["window_frames"]))
            return "video", [{**w, "paths": [os.path.join(dec, "keyframes", f"{n}.jpg") for n in w["frames"]]} for w in ws]
        return "", []

    def process(self, sink, file, ctx) -> None:
        if "caption" not in ctx.features:
            return
        o = {**DEFAULTS, **ctx.options_of("caption")}
        kind, units = self.units(file, ctx, o)
        if not units:
            return  # not an image or video, or no keyframes
        base, items = ctx.dir(), []
        for k, u in enumerate(units):
            name = f"window-{k:04d}.json"
            with open(os.path.join(base, name), "w") as f:
                json.dump({**u, "kind": kind}, f)
            items.append((os.path.join(base, name), name))
        members, captions = ctx.recurse_many(items, workers=int(o["workers"]), synthetic=True), []
        for u, m in zip(units, members):
            rec = None
            if m.claimed_by:
                with open(os.path.join(m.dir(m.claimed_by), "caption.json")) as f:
                    rec = json.load(f)
            rec = rec or {"text": "", "outcome": "invalid", "role": "none", "model": "", "reasons": ["not processed"]}
            if rec["outcome"] != "ok":
                sink.note(f"caption: {file.rel} {u['t_start']:.0f}s left out ({rec['outcome']}: {'; '.join(rec.get('reasons') or [])})"[:1000])
            captions.append({"t_start": u["t_start"], "t_end": u["t_end"], "text": rec["text"], "outcome": rec["outcome"], "role": rec["role"], "model": rec["model"], "frames": u["frames"]})
        out = json.dumps({"kind": kind, "captions": captions}).encode()
        sink.put_bytes(f"caption/{file.rel}/captions.json", out, "application/json")
        with open(os.path.join(base, "captions.json"), "wb") as f:  # scratch copy for media-markdown
            f.write(out)
