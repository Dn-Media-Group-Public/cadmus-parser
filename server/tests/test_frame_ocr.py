"""frame-ocr: pure pass choice / change detection / run merging, and an end-to-end run on a synthetic video with text appearing and disappearing."""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from cadmus_parser_server import frame_ocr as fo
from cadmus_parser_server import processors, worker
from conftest import MemSink

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg not installed")


def test_pick_pass():
    ps = [{"file": "frames/540-10.mp4", "fps": 10, "width": 960, "height": 540}, {"file": "frames/full-0.5.mkv", "fps": 0.5, "width": 1920, "height": 1080},
          {"file": "frames/360-1.mkv", "fps": 1, "width": 640, "height": 360}]
    assert fo.pick_pass(ps)["file"] == "frames/full-0.5.mkv"
    assert fo.pick_pass(ps[:1])["file"] == "frames/540-10.mp4"  # nothing at fps <= 1: the largest
    assert fo.pick_pass(ps, "360-1")["file"] == "frames/360-1.mkv"
    assert fo.pick_pass(ps, "nope") is None


def fr(seq):
    """seq: [(brightness, text)] one frame per second -> frames for ocr_frames; the brightness is the whole thumbnail."""
    return [(float(i), bytes([b]) * 8, (lambda t=t: t)) for i, (b, t) in enumerate(seq)]


def fake(text):
    return text, 71.0, "tesseract"


def test_unchanged_frames_are_not_ocred_and_extend_the_entry():
    calls = []
    out, capped = fo.ocr_frames(fr([(200, "HELLO"), (200, "HELLO"), (200, "HELLO"), (0, ""), (0, ""), (200, "BYE")]),
                                lambda t: (calls.append(t), fake(t))[1], 1.0, 0.02, 100)
    assert not capped and calls == ["HELLO", "", "BYE"]
    assert [(e["t_start"], e["t_end"], e["text"]) for e in out] == [(0.0, 3.0, "HELLO"), (5.0, 6.0, "BYE")]  # the unchanged black frame at t=4 does not extend an entry


def test_near_identical_text_merges_and_cap_stops():
    out, _ = fo.ocr_frames(fr([(0, "Hello world"), (100, "Hello w0rld"), (200, "other")]), fake, 1.0, 0.02, 100)
    assert [(e["t_start"], e["t_end"]) for e in out] == [(0.0, 2.0), (2.0, 3.0)]
    out, capped = fo.ocr_frames(fr([(0, "a"), (100, "bbbb"), (200, "cccccc")]), fake, 1.0, 0.02, 2)
    assert capped and len(out) == 2


@needs_ffmpeg
def test_end_to_end_text_appears_and_disappears(monkeypatch):
    pytest.importorskip("PIL")
    work, tree = tempfile.mkdtemp(), tempfile.mkdtemp()
    open(f"{tree}/original", "wb").write(b"original bytes")
    dec = f"{work}/media-decode/original"
    os.makedirs(f"{dec}/frames")
    seg = lambda c, d: ["-f", "lavfi", "-t", str(d), "-i", f"color=c={c}:s=64x48:r=1"]  # noqa: E731
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", *seg("white", 3), *seg("black", 2), *seg("white", 2), "-filter_complex", "concat=n=3:v=1:a=0",
                    "-c:v", "mjpeg", "-q:v", "3", f"{dec}/frames/full-1.mkv"], check=True)
    json.dump({"kind": "video", "frame_passes": [{"file": "frames/full-1.mkv", "codec": "mjpeg"}]}, open(f"{dec}/decode.json", "w"))
    monkeypatch.setenv("OCR_URL", "http://x")
    monkeypatch.setenv("OCR_TOKEN", "t")
    monkeypatch.setattr(fo, "_engine", lambda img: (("SIGN", 80.0, "tesseract") if sum(img.tobytes()) / (64 * 48) > 128 else ("", 0.0, "tesseract")))
    s = MemSink()
    worker.Runner(s, processors.order([fo.FrameOcr()]), {}, ["ocr"], work).run_tree(tree)
    out = json.loads(s.files["frame-ocr/original/ocr.json"])
    assert [(e["t_start"], e["t_end"], e["text"], e["engine"]) for e in out] == [(0.0, 3.0, "SIGN", "tesseract"), (5.0, 7.0, "SIGN", "tesseract")]
    s = MemSink()
    worker.Runner(s, processors.order([fo.FrameOcr()]), {}, [], work).run_tree(tree)  # feature not asked: nothing
    assert "frame-ocr/original/ocr.json" not in s.files
