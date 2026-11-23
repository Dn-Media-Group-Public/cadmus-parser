import json
import os
import sys

import httpx
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
import app as sidecar  # noqa: E402

PROFILES = {"stt": {"token": "ts", "base_url": "https://stt.example/v1", "model": "whisper", "auth": "bearer", "api_key": "K"},
            "llm": {"token": "tl", "base_url": "http://10.0.0.7/v1", "model": "gem", "backend": "chat", "max_output_tokens": 1000}}
H = {"Content-Type": "audio/ogg", "X-Chunk-Seconds": "300", "X-Language": "norsk", "X-Context": "Bj%C3%B8rn%20and%20Oslo"}


def client(answer, **kw):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=answer)
    return TestClient(sidecar.make_app(PROFILES, transport=httpx.MockTransport(upstream), **kw)), seen


def test_transcriptions_backend_returns_segments_and_sends_a_multipart_form():
    c, seen = client({"language": "norwegian", "segments": [{"start": 1.5, "end": 3, "text": " hei", "speaker": "A", "junk": 1}], "usage": {"seconds": 300}})
    r = c.post("/process", content=b"OGG", headers={**H, "Authorization": "Bearer ts"})
    assert r.json() == {"segments": [{"start": 1.5, "speaker": "A", "text": " hei"}], "raw": None, "finish_reason": None, "block_reason": None, "model": "whisper", "language": "norwegian"}
    req = seen[0]
    assert str(req.url) == "https://stt.example/v1/audio/transcriptions" and req.headers["authorization"] == "Bearer K"
    assert b'name="language"' in req.content and b"norsk" in req.content and "Bjørn".encode() in req.content  # context -> `prompt`, decoded


def test_chat_backend_attaches_the_audio_with_a_schema_and_a_cap():
    c, seen = client({"choices": [{"message": {"content": "{\"segments\": []}"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}})
    r = c.post("/process", content=b"OGG", headers={**H, "Authorization": "Bearer tl"})
    assert r.json()["raw"] == "{\"segments\": []}" and r.json()["finish_reason"] == "length"
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 1000 and body["response_format"]["type"] == "json_schema" and "authorization" not in seen[0].headers
    text = body["messages"][0]["content"][0]["text"]
    assert "05:00" in text and "norsk" in text and "Bjørn" in text and body["messages"][0]["content"][1]["type"] == "input_audio"


def test_auth_types_caps_and_upstream_failure():
    c, seen = client({}, max_body=10)
    assert c.post("/process", content=b"x", headers={**H, "Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/process", content=b"x", headers={**H, "Authorization": "Bearer ts", "Content-Type": "text/html"}).status_code == 415
    assert c.post("/process", content=b"x" * 11, headers={**H, "Authorization": "Bearer ts"}).status_code == 413
    assert not seen
    bad = TestClient(sidecar.make_app(PROFILES, transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert bad.post("/process", content=b"x", headers={**H, "Authorization": "Bearer ts"}).status_code == 502


def test_usage_is_recorded_against_the_job_token():
    c, _ = client({"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}, admin_token="adm")
    tok = c.post("/admin/jobs", json={"job": "j1", "profile": "llm"}, headers={"Authorization": "Bearer adm"}).json()["token"]
    c.post("/process", content=b"OGG", headers={**H, "Authorization": f"Bearer {tok}"})
    calls = c.get("/admin/jobs/j1/usage", headers={"Authorization": "Bearer adm"}).json()["calls"]
    assert [(x["feature"], x["profile"], x["outcome"], x["unit"], x["input"], x["output"]) for x in calls] == [("asr", "llm", "ok", "tokens", 5, 7)]
