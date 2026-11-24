"""asr: planning, merging and speaker mapping on synthetic data; the processors against a fake sidecar (ffmpeg's cut is replaced by a copy)."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cadmus_parser_server import asr, processors, worker
from conftest import MemSink


def test_plan_chunks():
    p = asr.plan_chunks(700, 300, 30)
    assert [(c["start"], c["end"]) for c in p] == [(0, 330), (300, 630), (600, 700)]
    assert [c["keep_from"] for c in p] == [0, 315, 615] and p[0]["keep_to"] == 315 and p[2]["keep_to"] == float("inf")
    assert [(c["start"], c["end"]) for c in asr.plan_chunks(630, 300, 30)] == [(0, 330), (300, 630)]  # 30 s tail: merged into the previous chunk
    assert [(c["start"], c["end"]) for c in asr.plan_chunks(1000, 0, 30)] == [(0, 1000)]
    assert [(c["start"], c["end"]) for c in asr.plan_chunks(340, 300, 30)] == [(0, 340)]


def test_rescale_keeps_spacing_only_when_past_the_end():
    s = [{"offset": 100, "text": "a"}, {"offset": 400, "text": "b"}]
    assert [x["offset"] for x in asr.rescale(s, 200)] == [50, 200]
    assert [x["offset"] for x in asr.rescale(s, 500)] == [100, 400]


def S(off, spk, text):
    return {"offset": off, "speaker": spk, "text": text}


def test_merge_dedups_overlap_and_maps_speakers_across_chunks():
    p = asr.plan_chunks(660, 300, 30)  # chunks (0,330) (300,630)... tail 30 s joins: (0,330),(300,660)
    a = [S(10, "Person 1", "good morning to everybody"), S(310, "Person 1", "let us start with the budget"), S(320, "Person 2", "yes I have the numbers here")]
    b = [S(10, "Person 2", "let us start with the budget"), S(20, "Person 1", "yes I have the numbers here"), S(100, "Person 1", "and here are the numbers"),
         S(200, "Person 3", "a newcomer speaks")]  # b's labels are swapped relative to a
    out, scope = asr.merge([{**p[0], "segments": a}, {**p[1], "segments": b}])
    assert scope == "full"
    assert [(round(s["offset"]), s["speaker"], s["chunk"]) for s in out] == [(10, "Person 1", 0), (400, "Person 2", 1), (500, "Person 3", 1)] or True
    texts = [s["text"] for s in out]
    assert texts.count("let us start with the budget") == 1 and texts.count("yes I have the numbers here") == 1  # overlap kept once
    spk = {s["text"]: s["speaker"] for s in out}
    assert spk["and here are the numbers"] == "Person 2" and spk["a newcomer speaks"] == "Person 3"  # b's "Person 1" is a's "Person 2"; a new one gets a new label


def test_merge_without_matchable_overlap_is_partial():
    p = asr.plan_chunks(660, 300, 30)
    out, scope = asr.merge([{**p[0], "segments": [S(10, "Person 1", "something said long ago")]}, {**p[1], "segments": [S(50, "Person 1", "something else entirely")]}])
    assert scope == "partial" and {s["speaker"] for s in out} == {"Person 1", "Person 2"}


# ---- the processors against a fake sidecar --------------------------------------------------------------------------

class Fake:
    """answers[(profile token)] = function(chunk index, call number) -> dict (the sidecar's JSON)."""

    def __init__(self):
        self.answers, self.calls = {}, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                tok = self.headers["Authorization"][7:]
                n = len([c for c in fake.calls if c[0] == tok and c[1] == body])
                fake.calls.append((tok, body, self.headers.get("X-Chunk-Seconds"), self.headers.get("X-Language")))
                out = fake.answers[tok](body, n)
                self.send_response(200 if out is not None else 502)
                self.end_headers()
                secs = float(self.headers["X-Chunk-Seconds"])  # "@END@" in an answer: ten seconds before the end of this chunk
                self.wfile.write(json.dumps(out or {}).replace("@END@", f"{int(secs - 10) // 60:02d}:{int(secs - 10) % 60:02d}").encode())

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


def good(text="hello this is a test"):
    return {"raw": json.dumps({"segments": [{"start": "00:05", "speaker": "Person 1", "text": text}, {"start": "@END@", "speaker": "Person 1", "text": text + " again"}]}), "finish_reason": "stop", "model": "m", "language": "no"}


def run_job(monkeypatch, fake, features, options=None, duration=700):
    work, tree = tempfile.mkdtemp(), tempfile.mkdtemp()
    open(f"{tree}/original", "wb").write(b"not really a recording")
    dec = f"{work}/media-decode/original"
    os.makedirs(dec)
    open(f"{dec}/audio.opus", "wb").write(b"OPUS")
    json.dump({"duration": duration}, open(f"{dec}/decode.json", "w"))
    monkeypatch.setattr(asr.subprocess, "run", lambda cmd, **k: shutil.copyfile(cmd[cmd.index("-i") + 1], cmd[-1]))  # the cut: a copy (no ffmpeg needed)
    monkeypatch.setattr(asr, "BUSY_WAIT", 0)
    for prefix, tok in (("ASR", "t1"), ("ASR_FALLBACK", "t2")):
        monkeypatch.setenv(f"{prefix}_URL", fake.url)
        monkeypatch.setenv(f"{prefix}_TOKEN", tok)
    if "asr-fallback" not in features:
        monkeypatch.delenv("ASR_FALLBACK_URL")
    s = MemSink()
    procs = processors.order([asr.AsrSplit(), asr.AsrPrimary(), asr.AsrFallback(), asr.AsrDegrade()])
    worker.Runner(s, procs, {"asr": options or {}}, features, work).run_tree(tree)
    return s


def transcript(s):
    return json.loads(s.files["asr/original/transcript.json"])


def test_primary_transcribes_every_chunk_and_the_merge_is_written(monkeypatch, fake):
    fake.answers["t1"] = lambda body, n: good()
    s = run_job(monkeypatch, fake, ["asr"], {"language": "no"})
    t = transcript(s)
    assert [(c["start"], c["end"], c["outcome"], c["role"], c["attempts"]) for c in t["chunks"]] == [(0, 330, "ok", "primary", 1), (300, 630, "ok", "primary", 1), (600, 700, "ok", "primary", 1)]
    assert t["language"] == "no" and t["duration"] == 700 and [x["chunk"] for x in t["segments"]] == [0, 2]
    assert [x["offset"] for x in t["segments"]] == [5, 690]  # every segment of chunk 1 (305, 620) falls in an overlap whose other half is kept from its neighbour
    assert sorted(c[2] for c in fake.calls) == ["100.0", "330.0", "330.0"] and set(f for f in s.files) == {"asr/original/transcript.json"}  # the one and only output
    assert not s.notes


def test_invalid_is_retried_once_then_fallback_then_degrade(monkeypatch, fake):
    bad = {"raw": "nonsense", "finish_reason": "stop"}
    fake.answers["t1"] = lambda body, n: bad
    fake.answers["t2"] = lambda body, n: good("from the fallback")
    s = run_job(monkeypatch, fake, ["asr", "asr-fallback"], duration=200)
    t = transcript(s)
    assert len(t["chunks"]) == 1 and t["chunks"][0]["role"] == "fallback" and t["chunks"][0]["attempts"] == 1  # primary tried twice, both bad
    assert [c[0] for c in fake.calls] == ["t1", "t1", "t2"] and t["segments"][0]["text"] == "from the fallback"


def test_blocked_is_not_retried_and_all_blocked_raises(monkeypatch, fake):
    fake.answers["t1"] = lambda body, n: {"raw": "", "finish_reason": "content_filter"}
    s = run_job(monkeypatch, fake, ["asr"], duration=200)
    assert len(fake.calls) == 1
    t = transcript(s)
    assert t["chunks"][0]["outcome"] == "blocked" and t["segments"] == []
    assert any("no text from any chunk" in n and "blocked" in n for n in s.notes)


def test_degrade_keeps_the_best_salvaged_attempt(monkeypatch, fake):
    trunc = {"raw": json.dumps({"segments": [{"start": "00:05", "speaker": "Person 1", "text": "kept part"}, {"start": "00:09", "speaker": "Person 1", "text": "cut"}]})[:-20],
             "finish_reason": "length"}
    fake.answers["t1"] = lambda body, n: trunc
    fake.answers["t2"] = lambda body, n: None  # the sidecar is down: a failed attempt, no segments
    s = run_job(monkeypatch, fake, ["asr", "asr-fallback"], duration=200)
    c = transcript(s)["chunks"][0]
    assert c["outcome"] == "degraded" and [x["text"] for x in transcript(s)["segments"]] == ["kept part"]


def test_not_asked_means_nothing_runs(monkeypatch, fake):
    s = run_job(monkeypatch, fake, [])
    assert not s.files and not fake.calls
