"""ASR (docs/implemented-plans/04-asr.md): `asr-split` cuts `audio.opus` (from media-decode) into chunk FILES and runs the processor chain on each as a synthetic
member; `asr-primary`, `asr-fallback` and `asr-degrade` (cadmus's own priority order) transcribe one chunk each through the ASR sidecar; `asr-split` then merges
the chunks into `asr/<file.rel>/transcript.json`. Chunk processors write no outputs: a chunk's result is a scratch file (`segments.json`, or `attempt.json` for a
failed attempt) in the claiming processor's `ctx.dir()`. Options: `options["asr"]` (shared by the four): chunk_seconds (300, 0 = whole file),
chunk_overlap_seconds (30), language, context, workers (4). Env: ASR_URL/ASR_TOKEN (job asked for `asr`), ASR_FALLBACK_URL/ASR_FALLBACK_TOKEN (`asr-fallback`)."""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from difflib import SequenceMatcher

from cadmus_parser_server import modelcheck

DEFAULTS = {"chunk_seconds": 300, "chunk_overlap_seconds": 30, "language": "", "context": "", "workers": 4}
MIN_TAIL_S = 60
TIMEOUT = int(os.environ.get("ASR_TIMEOUT") or 600)
BUSY_RETRIES, BUSY_WAIT = 5, float(os.environ.get("ASR_BUSY_WAIT") or 3)  # 503 = the sidecar is full: wait, not an attempt
MATCH_RATIO, MATCH_MIN_CHARS = 0.6, 12
CHUNK_NAME = re.compile(r"^chunk-\d{4,}\.opus$")
HANDLERS = ("sizecap", "archives", "markdown", "binary", "html", "apk", "documents")  # asr must see a file before these (the last one would try markitdown on audio)


# ---- pure helpers (tested on synthetic data) ------------------------------------------------------------------------

def plan_chunks(duration: float, chunk: float, overlap: float) -> list:
    """[{start, end, keep_from, keep_to}]: chunk k starts at k*chunk and runs `overlap` past its nominal end; a tail shorter than MIN_TAIL_S joins the previous chunk.
    A chunk's segments are kept from the middle of the overlap with the previous chunk up to the middle of the overlap with the next."""
    if chunk <= 0 or duration <= chunk:
        return [{"start": 0.0, "end": duration, "keep_from": 0.0, "keep_to": float("inf")}]
    starts = [i * chunk for i in range(int(duration // chunk) + (duration % chunk > 0))]
    if len(starts) > 1 and duration - starts[-1] < MIN_TAIL_S:
        starts.pop()
    out = [{"start": float(s), "end": float(duration if i == len(starts) - 1 else min(s + chunk + overlap, duration))} for i, s in enumerate(starts)]
    for k, c in enumerate(out):
        c["keep_from"] = 0.0 if k == 0 else (c["start"] + out[k - 1]["end"]) / 2
    for k, c in enumerate(out):
        c["keep_to"] = out[k + 1]["keep_from"] if k + 1 < len(out) else float("inf")
    return out


def rescale(segments: list, length: float) -> list:
    """A last timestamp past the chunk end scales the chunk's timeline to fit, keeping the relative spacing."""
    last = max((s["offset"] for s in segments), default=0)
    f = length / last if length > 0 and last > length else 1
    return [{**s, "offset": s["offset"] * f} for s in segments]


def map_speakers(prev: list, new: list, lo: float, hi: float, used: set) -> dict:
    """{label in `new`: canonical label}. Fuzzy-matches the text of both chunks' segments inside the overlap [lo, hi] (absolute time); one-to-one, weighted
    vote. An unmatched label becomes a fresh `Person N`. The result is a dict (the caller adds its values to `used`) with `.matches`, the number of matched labels."""
    votes: Counter = Counter()
    a_win = [s for s in prev if lo <= s["offset"] <= hi and s["speaker"] and len(s["text"]) >= MATCH_MIN_CHARS]
    for b in (s for s in new if lo <= s["offset"] <= hi and s["speaker"] and len(s["text"]) >= MATCH_MIN_CHARS):
        for a in a_win:
            r = SequenceMatcher(None, a["text"].lower(), b["text"].lower()).ratio()
            if r >= MATCH_RATIO:
                votes[(b["speaker"], a["speaker"])] += r * min(len(a["text"]), len(b["text"]))
    mapping, taken = {}, set()
    for (bl, al), _ in votes.most_common():
        if bl not in mapping and al not in taken:
            mapping[bl] = al
            taken.add(al)
    matches = len(mapping)
    n = 1
    for bl in sorted({s["speaker"] for s in new if s["speaker"]} - set(mapping)):
        while f"Person {n}" in used or f"Person {n}" in taken:
            n += 1
        mapping[bl] = f"Person {n}"
        taken.add(mapping[bl])
    return Mapping(mapping, matches)


class Mapping(dict):
    def __init__(self, d, matches):
        super().__init__(d)
        self.matches = matches


def merge(chunks: list) -> tuple[list, str]:
    """chunks: [{start, end, keep_from, keep_to, segments: [{offset (chunk-relative), speaker, text}]}] in order -> (segments with absolute offsets and a `chunk`
    index, speaker_scope 'full'|'partial')."""
    out, prev, used, partial = [], [], set(), False
    for k, c in enumerate(chunks):
        segs = [{**s, "offset": s["offset"] + c["start"]} for s in rescale(c["segments"], c["end"] - c["start"])]
        labels = {s["speaker"] for s in segs if s["speaker"]}
        if k == 0 or not used:
            mapping = {l: l for l in labels}
        else:
            mapping = map_speakers(prev, segs, c["start"], chunks[k - 1]["end"], used)
            partial = partial or (bool(labels) and mapping.matches == 0)
        used |= set(mapping.values())
        segs = [{**s, "speaker": mapping.get(s["speaker"], s["speaker"])} for s in segs]
        out += [{**s, "chunk": k} for s in segs if c["keep_from"] <= s["offset"] < c["keep_to"]]
        prev = segs
    return out, "partial" if partial else "full"


# ---- the sidecar call ---------------------------------------------------------------------------------------------

def call(prefix: str, audio_path: str, seconds: float, opts: dict) -> dict:
    """One POST /process to the sidecar of `<prefix>_URL`. Raises on any failure; waits (not an attempt) while the sidecar answers 503."""
    with open(audio_path, "rb") as f:
        body = f.read()
    headers = {"Authorization": f"Bearer {os.environ[prefix + '_TOKEN']}", "Content-Type": "audio/ogg", "X-Chunk-Seconds": f"{seconds:.1f}"}
    for key, hdr in (("language", "X-Language"), ("context", "X-Context")):
        if opts.get(key):
            headers[hdr] = urllib.parse.quote(" ".join(str(opts[key]).split()))
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


def _opts(ctx) -> dict:
    return {**DEFAULTS, **ctx.options_of("asr")}


def _chunk_of(file) -> dict | None:
    """The chunks.json entry of a chunk file made by asr-split, else None (an archive member that merely has such a name is not synthetic)."""
    if file.index_address is None or not CHUNK_NAME.match(file.name):
        return None
    try:
        with open(os.path.join(os.path.dirname(file.path), "chunks.json")) as f:
            return next((c for c in json.load(f)["chunks"] if c["name"] == file.name), None)
    except (OSError, ValueError, KeyError):
        return None


def attempts(prefix: str, file, ctx, chunk: dict, max_attempts: int) -> tuple[bool, dict]:
    """Up to `max_attempts` calls with validation; (success, record). The record always holds what is needed to claim (ok) or to salvage (not ok)."""
    length, results, o = chunk["end"] - chunk["start"], [], _opts(ctx)
    model = ""
    for _ in range(max_attempts):
        try:
            resp = call(prefix, file.path, length, o)
            model = resp.get("model") or model
            res = modelcheck.evaluate(resp, length)
            res.language = resp.get("language")
        except Exception as e:  # noqa: BLE001  an unreachable sidecar or a bad answer is a failed attempt
            res = modelcheck.Result("invalid", reason=f"call failed ({type(e).__name__})")
            res.language = None
        results.append(res)
        if res.status in ("ok", "blocked"):  # a block is deterministic for the same audio: retrying on the same model never helps
            break
    best = max(results, key=lambda r: len(r.segments))
    last = results[-1]
    rec = {"model": model, "attempts": len(results), "statuses": [r.status for r in results], "reasons": [r.reason for r in results if r.reason],
           "notes": [n for r in results for n in r.notes], "segments": best.segments, "language": next((r.language for r in results if r.language), None)}
    return last.status == "ok", {**rec, "segments": last.segments if last.status == "ok" else best.segments}


def _write(ctx, name: str, rec: dict) -> None:
    with open(os.path.join(ctx.dir(), name), "w") as f:
        json.dump(rec, f)


class _Chunk:
    """Shared by the three chunk processors: accept only chunk files made by asr-split."""
    top_dirs, before = ("asr",), HANDLERS
    env = "ASR"
    max_attempts = 2

    def accepts(self, file) -> bool:
        return bool(os.environ.get(self.env + "_URL") and os.environ.get(self.env + "_TOKEN")) and _chunk_of(file) is not None

    def process(self, sink, file, ctx) -> None:
        chunk = _chunk_of(file)
        ok, rec = attempts(self.env, file, ctx, chunk, self.max_attempts)
        rec["role"] = self.role
        if ok:
            _write(ctx, "segments.json", rec)
            ctx.claim()
        else:
            _write(ctx, "attempt.json", rec)
            sink.note(f"{self.name}: {file.name} failed ({'; '.join(rec['reasons']) or 'no result'})")


class AsrPrimary(_Chunk):
    name, role, env = "asr-primary", "primary", "ASR"


class AsrFallback(_Chunk):
    name, role, env, after = "asr-fallback", "fallback", "ASR_FALLBACK", ("asr-primary",)


class AsrDegrade:
    """A chunk nobody claimed: keep the salvaged attempt with the most segments (`degraded`), else an empty `blocked` / `failed` result. Always claims."""
    name, top_dirs, after, before = "asr-degrade", ("asr",), ("asr-fallback", "asr-primary"), HANDLERS

    def accepts(self, file) -> bool:
        return _chunk_of(file) is not None

    def process(self, sink, file, ctx) -> None:
        recs = []
        for proc in ("asr-primary", "asr-fallback"):
            p = os.path.join(ctx.work, proc, file.rel, "attempt.json")
            if os.path.exists(p):
                with open(p) as f:
                    recs.append(json.load(f))
        best = max(recs, key=lambda r: len(r["segments"]), default=None)
        statuses = [s for r in recs for s in r["statuses"]]
        if best and best["segments"]:
            rec = {**best, "outcome": "degraded"}
        else:
            rec = {"segments": [], "model": (best or {}).get("model", ""), "attempts": sum(r["attempts"] for r in recs), "role": "none", "statuses": statuses,
                   "reasons": [x for r in recs for x in r["reasons"]], "notes": [], "language": None,
                   "outcome": "blocked" if statuses and all(s == "blocked" for s in statuses) else "failed"}
        _write(ctx, "segments.json", rec)
        ctx.claim()


class AsrSplit:
    """The parent: cuts, recurses on every chunk (in parallel), merges, writes `transcript.json`. Runs only for a file with `audio.opus` from media-decode."""
    name, top_dirs, after, before = "asr-split", ("asr",), ("media-decode",), HANDLERS

    def accepts(self, file) -> bool:
        return bool(os.environ.get("ASR_URL") and os.environ.get("ASR_TOKEN")) and file.index_address is None

    def process(self, sink, file, ctx) -> None:
        if "asr" not in ctx.features:
            return
        dec = os.path.join(ctx.work, "media-decode", file.rel)
        audio = os.path.join(dec, "audio.opus")
        if not os.path.exists(audio):
            return  # not audio or video with sound
        with open(os.path.join(dec, "decode.json")) as f:
            duration = float(json.load(f).get("duration") or 0)
        if duration <= 0:
            sink.note(f"{file.rel}: asr skipped (unknown duration)")
            return
        o = _opts(ctx)
        plan = plan_chunks(duration, float(o["chunk_seconds"]), float(o["chunk_overlap_seconds"]))
        base, items = ctx.dir(), []
        for k, c in enumerate(plan):
            c["name"] = f"chunk-{k:04d}.opus"
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{c['start']:.3f}", "-t", f"{c['end'] - c['start']:.3f}",
                            "-i", audio, "-c", "copy", os.path.join(base, c["name"])], check=True, timeout=120, stdin=subprocess.DEVNULL)
            items.append((os.path.join(base, c["name"]), c["name"]))
        with open(os.path.join(base, "chunks.json"), "w") as f:
            json.dump({"chunks": [{**c, "keep_to": None if c["keep_to"] == float("inf") else c["keep_to"]} for c in plan]}, f)
        members = ctx.recurse_many(items, workers=int(o["workers"]), synthetic=True)
        infos, merge_in, langs = [], [], []
        for c, m in zip(plan, members):
            rec = None
            if m.claimed_by:
                with open(os.path.join(m.dir(m.claimed_by), "segments.json")) as f:
                    rec = json.load(f)
            rec = rec or {"segments": [], "model": "", "attempts": 0, "role": "none", "reasons": ["not processed"], "outcome": "failed", "language": None, "notes": []}
            langs.append(rec.get("language"))
            outcome = rec.get("outcome") or "ok"
            infos.append({"start": c["start"], "end": c["end"], "outcome": outcome, "role": rec["role"], "model": rec["model"], "attempts": rec["attempts"],
                          **({"reason": "; ".join(rec["reasons"])} if rec["reasons"] and outcome != "ok" else {}), **({"notes": rec["notes"]} if rec["notes"] else {})})
            merge_in.append({**c, "segments": rec["segments"]})
        segments, scope = merge(merge_in)
        doc = {"language": o["language"] or next((l for l in langs if l), None), "speaker_scope": scope, "duration": duration,
               "chunks": infos, "segments": [{**s, "offset": round(s["offset"], 1)} for s in segments]}
        sink.put_bytes(f"asr/{file.rel}/transcript.json", json.dumps(doc).encode(), "application/json")
        with open(os.path.join(base, "transcript.json"), "w") as f:  # scratch copy for media-markdown
            json.dump(doc, f)
        if not any(s["text"] for s in segments):
            reasons = sorted({i.get("reason") or i["outcome"] for i in infos})
            raise RuntimeError(f"asr: no text from any chunk ({'; '.join(reasons)})"[:500])
