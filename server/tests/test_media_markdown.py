"""media-markdown: pure layout, and the processor on scratch files (claims media, writes only when there is a part, leaves non-media alone)."""

import json
import os

from cadmus_parser_server import media_markdown as mm
from cadmus_parser_server import processors, worker
from conftest import MemSink

CAP = [{"t_start": 0, "t_end": 20, "text": "A room.", "outcome": "ok"}, {"t_start": 20, "t_end": 40, "text": "", "outcome": "blocked"}, {"t_start": 40, "t_end": 60, "text": "A cat.", "outcome": "ok"}]
OCR = [{"t_start": 5, "text": "HELLO"}, {"t_start": 45, "text": "BYE"}]
SEG = [{"offset": 3, "text": "hi", "speaker": "Person 1"}, {"offset": 42, "text": "meow"}]


def test_all_three_in_time_order():
    md = mm.build_markdown("video", CAP, OCR, SEG)
    assert md.split("\n\n") == ["`0:00` _SCENE_: A room.", "`0:03` **Person 1**: hi", "`0:05` _ON SCREEN_: HELLO", "`0:40` _SCENE_: A cat.",
                                  "`0:42` meow", "`0:45` _ON SCREEN_: BYE"]  # the blocked caption is left out; no speaker, no name


def test_single_parts_and_multiline_ocr():
    assert mm.build_markdown("audio", [], [], SEG).count("**") == 2 and "ON SCREEN" not in mm.build_markdown("video", [], [], SEG)
    assert mm.build_markdown("video", [], [{"t_start": 3725, "text": "A\n\nB"}], []) == "`1:02:05` _ON SCREEN_: A / B"
    assert mm.build_markdown("video", [], [], []) == ""


def test_image():
    assert mm.build_markdown("image", CAP[:1], [], [], "Hello") == "_SCENE_: A room.\n\n_ON SCREEN_: Hello"
    assert mm.build_markdown("image", [], [], [], "Hello") == "_ON SCREEN_: Hello"


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
    monkeypatch.setattr(mm.page_ocr, "image_markdown", lambda *a: "Hello")
    s = run(tmp_path, "a.png", {"kind": "image"}, [("caption-split", "captions.json", {"captions": CAP[:1]})])
    assert s.files["parsed/a.png.md"] == b"_SCENE_: A room.\n\n_ON SCREEN_: Hello"
