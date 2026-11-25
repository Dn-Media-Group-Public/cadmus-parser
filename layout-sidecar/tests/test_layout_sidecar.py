import io
import json
import os
import sys

import httpx
import numpy as np
from PIL import Image
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
import app as sidecar  # noqa: E402


def png(w=200, h=100) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(b, "PNG")
    return b.getvalue()


def hdr(tok="t", ct="image/png", **kw):
    return {"Authorization": f"Bearer {tok}", "Content-Type": ct, **kw}


class Stub:  # an onnxruntime session: end-to-end output (1, N, 6) in the 640x640 letterboxed frame
    def __init__(self, rows):
        self.rows = rows

    def get_inputs(self):
        return [type("I", (), {"name": "images"})()]

    def run(self, _, feed):
        assert feed["images"].shape == (1, 3, 640, 640)
        return [np.array([self.rows], dtype=np.float32)]


def test_yolo_boxes_map_back_to_image_pixels_and_labels(monkeypatch):
    # a 200x100 page is scaled x3.2 (640x320) and padded 160 px top and bottom: a box at (320..640, 160+0..160+160) is the right half, top half
    monkeypatch.setattr(sidecar, "load_session", lambda path: Stub([[320, 160, 640, 320, 0.9, 3], [0, 160, 64, 192, 0.8, 1], [0, 0, 5, 5, 0.1, 3]]))
    c = TestClient(sidecar.make_app({"p": {"token": "t", "engine": "yolo-onnx", "model_path": "/models/m.onnx"}}))
    r = c.post("/process", content=png(), headers=hdr())
    assert r.status_code == 200
    d = r.json()
    assert (d["engine"], d["width"], d["height"]) == ("yolo-onnx", 200, 100) and "score" not in d
    assert d["boxes"] == [{"label": "picture", "bbox": [100.0, 0.0, 200.0, 50.0], "score": 0.9}, {"label": "text", "bbox": [0.0, 0.0, 20.0, 10.0], "score": 0.8}]  # the 0.1 box is under score_min
    assert d["escalate_below"] == 0.5 and d["max_figure_members"] == 2000


def test_auth_type_and_bad_image():
    c = TestClient(sidecar.make_app({"p": {"token": "t", "engine": "openai-vlm", "base_url": "http://v", "model": "m"}}))
    assert c.post("/process", content=png(), headers=hdr("x")).status_code == 401
    assert c.post("/process", content=b"x", headers=hdr(ct="text/plain")).status_code == 415
    assert c.post("/process", content=b"notpng", headers=hdr()).status_code == 400


DOC = {"document": {"json_content": {
    "pages": {"1": {"size": {"width": 100, "height": 50}}},
    "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/groups/0"}, {"$ref": "#/tables/0"}]},
    "groups": [{"children": [{"$ref": "#/pictures/0"}, {"$ref": "#/texts/1"}]}],
    "texts": [{"label": "section_header", "text": "Intro", "prov": [{"page_no": 1, "bbox": {"l": 0, "t": 50, "r": 100, "b": 40, "coord_origin": "BOTTOMLEFT"}}]},
              {"label": "text", "text": "Body", "prov": [{"page_no": 1, "bbox": {"l": 0, "t": 20, "r": 50, "b": 10, "coord_origin": "BOTTOMLEFT"}}]}],
    "pictures": [{"prov": [{"page_no": 1, "bbox": {"l": 50, "t": 35, "r": 100, "b": 15, "coord_origin": "BOTTOMLEFT"}}]}],
    "tables": [{"prov": [{"page_no": 1, "bbox": {"l": 0, "t": 10, "r": 100, "b": 0, "coord_origin": "BOTTOMLEFT"}}],
                "data": {"num_rows": 2, "num_cols": 2, "table_cells": [{"start_row_offset_idx": 0, "start_col_offset_idx": 0, "text": "a"}, {"start_row_offset_idx": 0, "start_col_offset_idx": 1, "text": "b"},
                                                                         {"start_row_offset_idx": 1, "start_col_offset_idx": 0, "text": "1"}, {"start_row_offset_idx": 1, "start_col_offset_idx": 1, "text": "2"}]}}]}},
    "confidence": {"mean_score": 0.8}}


def docling(mode, seen):
    def up(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json=DOC)
    prof = {"p": {"token": "t", "engine": "docling-serve", "base_url": "http://d", "mode": mode, "lang_map": {"nb": "no"}}}
    return TestClient(sidecar.make_app(prof, transport=httpx.MockTransport(up)))


def test_docling_layout_mode_converts_points_to_pixels_in_reading_order():
    seen = []
    r = docling("layout", seen).post("/process", content=png(200, 100), headers=hdr(**{"X-Language": "nb"}))  # the page is 100x50 pt sent at 200x100 px: x2, y flipped
    d = r.json()
    assert str(seen[0].url) == "http://d/v1/convert/file" and b'name="do_ocr"\r\n\r\nfalse' in seen[0].content and b"ocr_lang" not in seen[0].content
    assert [b["label"] for b in d["boxes"]] == ["title", "picture", "text", "table"] and d["score"] == 0.8 and "full" not in d
    assert d["boxes"][1]["bbox"] == [100.0, 30.0, 200.0, 70.0] and all("markdown" not in b for b in d["boxes"])
    assert d["escalate_below"] == 0.5 and d["max_figure_members"] == 1000


def test_docling_full_mode_has_markdown_and_ocr_language_through_lang_map():
    seen = []
    d = docling("full", seen).post("/process", content=png(200, 100), headers=hdr(**{"X-Language": "nb"})).json()
    assert b'name="do_ocr"\r\n\r\ntrue' in seen[0].content and b'name="ocr_lang"\r\n\r\nno' in seen[0].content
    assert d["full"] is True and [b.get("markdown") for b in d["boxes"]] == ["## Intro", None, "Body", "| a | b |\n|---|---|\n| 1 | 2 |"]


def vlm(text, **p):
    up = lambda req: httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}})  # noqa: E731
    prof = {"p": {"token": "t", "engine": "openai-vlm", "base_url": "http://v", "model": "m", **p}}
    return TestClient(sidecar.make_app(prof, transport=httpx.MockTransport(up)))


def test_vlm_json_and_doctags_and_garbage_is_a_502():
    r = vlm('Sure:\n```json\n{"boxes": [{"label": "picture", "bbox": [10, 20, 60, 80]}]}\n```').post("/process", content=png(), headers=hdr())
    assert r.json()["boxes"] == [{"label": "picture", "bbox": [10.0, 20.0, 60.0, 80.0], "score": 1.0}] and r.json()["escalate_below"] is None and r.json()["max_figure_members"] == 200
    assert vlm('{"boxes": []}').post("/process", content=png(), headers=hdr()).json()["boxes"] == []  # empty is an answer
    r = vlm("<doctag><picture><loc_50><loc_100><loc_250><loc_400></picture></doctag>", format="doctags").post("/process", content=png(200, 100), headers=hdr())
    assert r.json()["boxes"][0]["bbox"] == [20.0, 20.0, 100.0, 80.0]
    assert vlm("I see a cat").post("/process", content=png(), headers=hdr()).status_code == 502


def test_usage_is_recorded_per_job():
    up = lambda req: httpx.Response(200, json={"choices": [{"message": {"content": '{"boxes": []}'}}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}})  # noqa: E731
    c = TestClient(sidecar.make_app({"p": {"token": "t", "engine": "openai-vlm", "base_url": "http://v", "model": "m"}}, transport=httpx.MockTransport(up), admin_token="adm"))
    tok = c.post("/admin/jobs", json={"job": "j1", "profile": "p"}, headers={"Authorization": "Bearer adm"}).json()["token"]
    c.post("/process", content=png(), headers=hdr(tok))
    calls = c.get("/admin/jobs/j1/usage", headers={"Authorization": "Bearer adm"}).json()["calls"]
    assert calls[0]["feature"] == "layout" and (calls[0]["input"], calls[0]["output"], calls[0]["unit"]) == (3, 4, "tokens")


def test_bad_profiles_refused():
    import pytest
    for p in ({"token": "t", "engine": "nope"}, {"token": "t", "engine": "yolo-onnx"}, {"token": "t", "engine": "docling-serve"}):
        with pytest.raises(ValueError):
            sidecar.make_app({"p": p})
