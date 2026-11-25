"""media-markdown: pure layout, and the processor on scratch files (claims media, writes only when there is a part, leaves non-media alone)."""

import json
import os

from cadmus_parser_server import media_markdown as mm
from cadmus_parser_server import processors, worker
from conftest import MemSink

CAP = [{"t_start": 0, "t_end": 20, "text": "A room.", "outcome": "ok"}, {"t_start": 20, "t_end": 40, "text": "", "outcome": "blocked"}, {"t_start": 40, "t_end": 60, "text": "A cat.", "outcome": "ok"}]
OCR = [{"t_start": 5, "text": "HELLO"}, {"t_start": 45, "text": "BYE"}]
SEG = [{"offset": 3, "text": "hi", "speaker": "Person 1"}, {"offset": 42, "text": "meow"}]


def test_all_three_by_caption_windows():
    md = mm.build_markdown("video", CAP, OCR, SEG)
    assert md.index("**Scene:** A room.") < md.index("HELLO") < md.index("Person 1: hi") < md.index("## 0:40") < md.index("A cat.") < md.index("BYE") < md.index("meow")
    assert "## 0:20" not in md and md.count("**Scene:**") == 2  # the blocked unit has a window but no text, so no section


def test_fixed_windows_and_single_parts():
    assert "## 0:30" in mm.build_markdown("video", [], [], [{"offset": 31, "text": "x"}]) and "## 0:00" not in mm.build_markdown("video", [], [], [{"offset": 31, "text": "x"}])
    assert mm.build_markdown("audio", [], [], SEG).count("**Speech:**") == 2 and "On screen" not in mm.build_markdown("video", [], [], SEG)
    assert "**On screen:** HELLO" in mm.build_markdown("video", [], OCR, [])
    assert mm.build_markdown("video", [], [], []) == ""


def test_image():
    assert mm.build_markdown("image", CAP[:1], [], [], image_text="Hello") == "**Scene:** A room.\n\nHello"
    assert mm.build_markdown("image", [], [], [], image_text="Hello") == "Hello"


def run(tmp_path, name, decode, scratch, monkeypatch=None):
    tree, work = tmp_path / "in", tmp_path / "work"
    tree.mkdir(parents=True)
    (tree / name).write_bytes(b"\x89PNG\0\0")
    for proc, fn, data in [("media-decode", "decode.json", decode), *scratch]:
        if data is not None:
            d = work / proc / name
            d.mkdir(parents=True, exist_ok=True)
            (d / fn).write_text(json.dumps(data))
    s = MemSink()
    r = worker.Runner(s, processors.order([mm.MediaMarkdown()]), {}, [], str(work))
    r.run_tree(str(tree))
    return s


def test_processor(tmp_path):
    s = run(tmp_path / "a", "v.mp4", {"kind": "video"}, [("asr-split", "transcript.json", {"segments": SEG}), ("frame-ocr", "ocr.json", OCR)])
    assert b"meow" in s.files["parsed/v.mp4.md"]
    s = run(tmp_path / "b", "v.mp4", {"kind": "video"}, [])
    assert not s.files and not any("not parsed" in n for n in s.notes)  # claimed, nothing to write
    s = run(tmp_path / "c", "x.bin", None, [])
    assert not s.files  # not media: returns without claiming (a later handler would take it)


def test_image_with_caption(tmp_path, monkeypatch):
    monkeypatch.setattr(mm.ocr, "maybe_ocr", lambda *a: "Hello")
    s = run(tmp_path, "a.png", {"kind": "image"}, [("caption-split", "captions.json", {"captions": CAP[:1]})])
    assert s.files["parsed/a.png.md"] == b"**Scene:** A room.\n\nHello"
