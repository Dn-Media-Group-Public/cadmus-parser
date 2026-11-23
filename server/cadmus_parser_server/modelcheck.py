"""Validation of one model answer (docs/implemented-plans/04-asr.md), shared by the processors that call a model sidecar. Pure functions, no I/O.
`evaluate(resp, chunk_seconds)` -> Result(status ok|invalid|blocked, segments, reason, notes). The first failure decides."""

import json
import re
from collections import Counter
from dataclasses import dataclass, field

BLOCKED_FINISH = {"content_filter", "safety", "prohibited_content", "blocklist", "spii", "image_safety", "blocked"}
NORMAL_FINISH = {None, "", "stop", "end_turn", "eos", "complete"}
MIN_TOLERANCE_S, TOLERANCE_FRACTION, NOTE_GAP_S = 120.0, 0.2, 60.0
LOOP_WORDS, LOOP_MIN_DISTINCT, LOOP_REPEATS, WORD_RUN = 8, 3, 4, 11


@dataclass
class Result:
    status: str  # ok | invalid | blocked
    segments: list = field(default_factory=list)  # [{"offset": float, "speaker": str | None, "text": str}], what could be salvaged
    reason: str = ""
    notes: list = field(default_factory=list)


def parse_time(v) -> float | None:
    """Seconds from a number or `SS`, `MM:SS`, `H:MM:SS` (fractions allowed); None if it is not a time."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v >= 0 else None
    if isinstance(v, str):
        try:
            t = 0.0
            for part in v.strip().split(":"):
                t = t * 60 + float(part)
            return t if t >= 0 and len(v.split(":")) <= 3 else None
        except ValueError:
            return None
    return None


def segment(el) -> dict | None:
    """One schema-checked segment from a model element (`start` or `offset`, optional `speaker`, `text`), or None."""
    if not isinstance(el, dict) or not isinstance(el.get("text"), str):
        return None
    t = parse_time(el.get("start", el.get("offset")))
    spk = el.get("speaker")
    if t is None or not (spk is None or isinstance(spk, (str, int))):
        return None
    return {"offset": t, "speaker": None if spk is None else str(spk), "text": el["text"].strip()}


def decode_elements(raw: str) -> list:
    """The complete leading elements of the first JSON array in `raw`, one `raw_decode` at a time, so a truncated answer keeps what was whole."""
    i = raw.find("[")
    if i < 0:
        return []
    dec, out, n = json.JSONDecoder(), [], len(raw)
    i += 1
    while i < n:
        while i < n and raw[i] in " \t\r\n,":
            i += 1
        if i >= n or raw[i] == "]":
            break
        try:
            el, i = dec.raw_decode(raw, i)
        except ValueError:
            break  # the truncated element; nothing after it can be trusted
        out.append(el)
    return out


def repetition(segments: list) -> str | None:
    words = re.findall(r"\w+", " ".join(s["text"] for s in segments).lower())
    run = 1
    for a, b in zip(words, words[1:]):
        run = run + 1 if a == b else 1
        if run >= WORD_RUN:
            return f"the word {a!r} repeated {WORD_RUN} times in a row"
    grams = Counter(tuple(words[i:i + LOOP_WORDS]) for i in range(len(words) - LOOP_WORDS + 1))
    for g, c in grams.items():
        if c >= LOOP_REPEATS and len(set(g)) >= LOOP_MIN_DISTINCT:
            return f"repetition loop ({' '.join(g)!r} x{c})"
    return None


def evaluate(resp: dict, chunk_seconds: float) -> Result:
    """`resp` is the sidecar's JSON: `segments` (a list, transcriptions backend) or `raw` (the model's text, chat backend), `finish_reason`, `block_reason`."""
    finish = resp.get("finish_reason")
    f = finish.lower() if isinstance(finish, str) else finish
    if resp.get("block_reason") or f in BLOCKED_FINISH:
        return Result("blocked", reason=f"blocked ({resp.get('block_reason') or finish})")
    els = resp["segments"] if isinstance(resp.get("segments"), list) else decode_elements(resp.get("raw") or "")
    segs = [s for s in map(segment, els) if s]
    if not segs:
        return Result("invalid", reason="no parseable segments")
    notes: list = []
    if f not in NORMAL_FINISH:
        return Result("invalid", segs, f"truncated (finish reason {finish})")
    if any(b["offset"] < a["offset"] for a, b in zip(segs, segs[1:])):
        return Result("invalid", segs, "timestamps go backwards")
    if chunk_seconds > 0:
        gap = chunk_seconds - segs[-1]["offset"]
        if gap > max(MIN_TOLERANCE_S, TOLERANCE_FRACTION * chunk_seconds):
            return Result("invalid", segs, f"last timestamp {gap:.0f} s before the chunk end")
        if gap > NOTE_GAP_S:
            notes.append(f"last timestamp {gap:.0f} s before the chunk end")
    if (why := repetition(segs)):
        return Result("invalid", segs, why)
    return Result("ok", segs, notes=notes)
