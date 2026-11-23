"""caption: window planning on synthetic data; the processors against a fake sidecar (needs Pillow for the frame scaling)."""

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image

from cadmus_parser_server import caption, modelcheck, processors, worker
from conftest import MemSink


def test_plan_windows():
    kf = [{"n": i, "t": i * 10.0} for i in range(5)]
    w = caption.plan_windows(kf, 100.0, 2)
    assert [(x["t_start"], x["t_end"], x["frames"]) for x in w] == [(0, 20, [0, 1]), (20, 40, [2, 3]), (40, 100, [4])]
    assert len(caption.plan_windows(kf, 100.0, 99)) == 1  # capped at the sidecar's 20, so 5 fit


def test_evaluate_text():
    assert modelcheck.evaluate_text({"text": " a dog ", "finish_reason": "stop"}).text == "a dog"
    assert modelcheck.evaluate_text({"text": "", "finish_reason": "content_filter"}).status == "blocked"
    assert modelcheck.evaluate_text({"text": "cut off", "finish_reason": "length"}).status == "invalid"
    assert modelcheck.evaluate_text({"text": "  ", "finish_reason": "stop"}).status == "invalid"
    assert modelcheck.evaluate_text({"text": "no " * 20, "finish_reason": "stop"}).status == "invalid"


class Fake:
    """answers[token] = function(call number, content type, frame times) -> dict (the sidecar's JSON) or None (a 502)."""

    def __init__(self):
        self.answers, self.calls = {}, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                tok = self.headers["Authorization"][7:]
                n = len([c for c in fake.calls if c[0] == tok])
                fake.calls.append((tok, self.headers["Content-Type"], self.headers.get("X-Frame-Times")))
                out = fake.answers[tok](n)
                self.send_response(200 if out is not None else 502)
                self.end_headers()
                self.wfile.write(json.dumps(out or {}).encode())

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.srv.shutdown()


def good(text="a red car on a street"):
    return {"text": text, "finish_reason": "stop", "model": "m"}


def run_job(monkeypatch, fake, features, kind="image", n_keyframes=3, options=None):
    work, tree = tempfile.mkdtemp(), tempfile.mkdtemp()
    open(f"{tree}/original", "wb").write(b"original bytes")
    dec = f"{work}/media-decode/original"
    os.makedirs(f"{dec}/keyframes")
    d = {"kind": kind, "duration": 100.0}
    if kind == "image":
        Image.new("RGB", (40, 30)).save(f"{tree}/original", "PNG")
        d["renditions"] = {"web": "original"}
    else:
        for i in range(n_keyframes):
            Image.new("RGB", (40, 30)).save(f"{dec}/keyframes/{i}.jpg")
        json.dump([{"n": i, "t": i * 10.0} for i in range(n_keyframes)], open(f"{dec}/keyframes.json", "w"))
    json.dump(d, open(f"{dec}/decode.json", "w"))
    monkeypatch.setattr(caption, "BUSY_WAIT", 0)
    for prefix, tok in (("CAPTION", "t1"), ("CAPTION_FALLBACK", "t2")):
        monkeypatch.setenv(f"{prefix}_URL", fake.url)
        monkeypatch.setenv(f"{prefix}_TOKEN", tok)
    if "caption-fallback" not in features:
        monkeypatch.delenv("CAPTION_FALLBACK_URL")
    s = MemSink()
    procs = processors.order([caption.CaptionSplit(), caption.CaptionPrimary(), caption.CaptionFallback(), caption.CaptionSkip()])
    worker.Runner(s, procs, {"caption": options or {}}, features, work).run_tree(tree)
    return s


def captions(s):
    return json.loads(s.files["caption/original/captions.json"])


def test_image_is_one_unit(monkeypatch, fake):
    fake.answers["t1"] = lambda n: good()
    s = run_job(monkeypatch, fake, ["caption"])
    c = captions(s)
    assert c["kind"] == "image" and c["captions"] == [{"t_start": 0, "t_end": 0, "text": "a red car on a street", "outcome": "ok", "role": "primary", "model": "m", "frames": [0]}]
    assert fake.calls == [("t1", "image/jpeg", None)] and list(s.files) == ["caption/original/captions.json"] and not s.notes


def test_video_windows_are_multipart_with_times(monkeypatch, fake):
    fake.answers["t1"] = lambda n: good(f"window {n}")
    s = run_job(monkeypatch, fake, ["caption"], "video", 5, {"window_frames": 2, "workers": 1})
    c = captions(s)
    assert [(x["t_start"], x["t_end"], x["frames"]) for x in c["captions"]] == [(0, 20, [0, 1]), (20, 40, [2, 3]), (40, 100, [4])]
    assert fake.calls[0][1].startswith("multipart/mixed") and fake.calls[0][2] == "0.00,10.00"


def test_invalid_retried_then_fallback_then_skipped(monkeypatch, fake):
    fake.answers["t1"] = lambda n: {"text": "", "finish_reason": "stop"}
    fake.answers["t2"] = lambda n: good("from the fallback")
    s = run_job(monkeypatch, fake, ["caption", "caption-fallback"])
    assert captions(s)["captions"][0]["role"] == "fallback" and [c[0] for c in fake.calls] == ["t1", "t1", "t2"]
    fake.calls.clear()
    fake.answers["t2"] = lambda n: None  # down
    s = run_job(monkeypatch, fake, ["caption", "caption-fallback"])
    c = captions(s)["captions"][0]
    assert c["outcome"] == "invalid" and c["text"] == "" and any("left out" in n for n in s.notes)  # noted, the job does not fail


def test_blocked_is_not_retried(monkeypatch, fake):
    fake.answers["t1"] = lambda n: {"text": "", "finish_reason": "content_filter"}
    s = run_job(monkeypatch, fake, ["caption"])
    assert len(fake.calls) == 1 and captions(s)["captions"][0]["outcome"] == "blocked"


def test_not_asked_means_nothing_runs(monkeypatch, fake):
    s = run_job(monkeypatch, fake, [])
    assert not s.files and not fake.calls
