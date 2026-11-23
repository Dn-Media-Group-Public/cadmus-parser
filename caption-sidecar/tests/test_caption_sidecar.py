import json
import os
import sys

import httpx
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
import app as sidecar  # noqa: E402

PROFILES = {"vis": {"token": "tv", "base_url": "https://vis.example/v1", "model": "gem", "auth": "bearer", "api_key": "K", "max_output_tokens": 500}}
ANSWER = {"choices": [{"message": {"content": "a *cat*"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}


def client(answer=ANSWER, **kw):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=answer)
    return TestClient(sidecar.make_app(PROFILES, transport=httpx.MockTransport(upstream), **kw)), seen


def multipart(parts):
    b = b"".join(b"--XX\r\nContent-Type: image/jpeg\r\n\r\n" + p + b"\r\n" for p in parts) + b"--XX--\r\n"
    return b, "multipart/mixed; boundary=XX"


def test_image_goes_upstream_as_a_data_uri():
    c, seen = client()
    r = c.post("/process", content=b"JPEG", headers={"Authorization": "Bearer tv", "Content-Type": "image/jpeg"})
    assert r.json() == {"text": "a *cat*", "finish_reason": "stop", "block_reason": None, "model": "gem"}
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://vis.example/v1/chat/completions" and req.headers["authorization"] == "Bearer K" and body["max_tokens"] == 500
    assert body["messages"][0]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,SlBFRw=="


def test_window_interleaves_times_and_frames():
    c, seen = client()
    body, ct = multipart([b"A", b"B"])
    r = c.post("/process", content=body, headers={"Authorization": "Bearer tv", "Content-Type": ct, "X-Frame-Times": "0.0,65.5"})
    assert r.status_code == 200
    content = json.loads(seen[0].content)["messages"][0]["content"]
    assert [x["type"] for x in content] == ["text", "text", "image_url", "text", "image_url"] and content[1]["text"] == "[00:00]" and content[3]["text"] == "[01:05]"


def test_blocked_and_errors():
    c, seen = client({"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]})
    assert c.post("/process", content=b"x", headers={"Authorization": "Bearer tv", "Content-Type": "image/png"}).json()["finish_reason"] == "content_filter"
    c, seen = client(max_body=10)
    h = {"Authorization": "Bearer tv", "Content-Type": "image/png"}
    assert c.post("/process", content=b"x", headers={**h, "Authorization": "Bearer no"}).status_code == 401
    assert c.post("/process", content=b"x", headers={**h, "Content-Type": "text/html"}).status_code == 415
    assert c.post("/process", content=b"x" * 11, headers=h).status_code == 413
    assert c.post("/process", content=b"--XX--", headers={**h, "Content-Type": "multipart/mixed; boundary=XX", "X-Frame-Times": "1"}).status_code == 400
    assert not seen
