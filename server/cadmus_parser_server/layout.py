"""Layout chain client (docs/implemented-plans/figure-extraction.md): the regions on a page image from the profiles the job named, in order, through the layout sidecar's protocol.
The wrapper puts LAYOUT_CHAIN = JSON [{name, url, token}, ...] in the environment, exactly the profiles the job asked for; without it there is no layout code path.
A page goes to the next profile when a profile's answer is unreliable (no box of any label, or a confidence below the profile's own `escalate_below`, which the sidecar reports
with the answer) and on any error; 503 (the sidecar is full) is waited out, not counted. The last profile's answer stands."""

import json
import os
import time
import urllib.error
import urllib.request

TIMEOUT = int(os.environ.get("LAYOUT_TIMEOUT") or 120)
BUSY_RETRIES, BUSY_WAIT = 5, float(os.environ.get("LAYOUT_BUSY_WAIT") or 3)


def chain() -> list:
    return json.loads(os.environ.get("LAYOUT_CHAIN") or "[]")


def enabled() -> bool:
    return bool(chain())


def call(entry: dict, png: bytes, langs: list) -> dict:
    """One POST /process of the profile `entry`. Raises on any failure; waits while the sidecar answers 503."""
    headers = {"Authorization": f"Bearer {entry['token']}", "Content-Type": "image/png", **({"X-Language": ",".join(langs)} if langs else {})}
    for i in range(BUSY_RETRIES + 1):
        req = urllib.request.Request(entry["url"].rstrip("/") + "/process", data=png, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 503 or i == BUSY_RETRIES:
                raise
            time.sleep(BUSY_WAIT * (i + 1))
    raise RuntimeError("unreachable")


def confidence(ans: dict) -> float | None:
    """The engine's page-level score, else the mean score of its boxes, else None (no boxes)."""
    if ans.get("score") is not None:
        return float(ans["score"])
    s = [float(b["score"]) for b in ans["boxes"] if b.get("score") is not None]
    return sum(s) / len(s) if s else None


def reliable(ans: dict) -> bool:
    if not ans["boxes"]:
        return False  # a page without pictures is normal, but a page without any region at all is a detector that saw nothing
    below, c = ans.get("escalate_below"), confidence(ans)
    return below is None or c is None or c >= float(below)


def detect(png: bytes, langs: list, call_fn=None) -> tuple[dict | None, str, list]:
    """(answer, profile name, notes) of the first reliable answer, else of the last profile's answer; (None, "", notes) when nothing answered."""
    call_fn, notes, tried = call_fn or call, [], []
    ch = chain()
    for i, entry in enumerate(ch):
        try:
            ans = call_fn(entry, png, langs)
            ans["boxes"]  # noqa: B018  a malformed answer is a failed attempt
        except Exception as e:  # noqa: BLE001  down, token, 502 (unparseable upstream), bad JSON
            tried.append(entry["name"])
            notes.append(f"layout {entry['name']} failed ({type(e).__name__})")
            continue
        if reliable(ans) or i == len(ch) - 1:
            return ans, entry["name"], [*notes, f"layout {entry['name']}" + (f" (escalated from {', '.join(tried)})" if tried else "")]
        tried.append(entry["name"])
        notes.append(f"layout {entry['name']} unsure")
    return None, "", notes
