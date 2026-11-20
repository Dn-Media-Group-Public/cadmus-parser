import json
import os
import sys

import httpx
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
import app as sidecar  # noqa: E402
import egress_rules  # noqa: E402

PROFILES = {"remote": {"token": "tr", "base_url": "https://llm.example/v1", "model": "m1", "auth": "bearer", "api_key": "K"},
            "lan": {"token": "tl", "base_url": "http://10.0.0.7:8000/v1", "model": "m2", "auth": "none"}}
PNG = b"\x89PNG\r\n"


def client(**kw):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello text"}}]})
    return TestClient(sidecar.make_app(PROFILES, transport=httpx.MockTransport(upstream), **kw)), seen


def test_token_picks_the_profile_and_nothing_else_is_accepted():
    c, seen = client()
    r = c.post("/process", content=PNG, headers={"Authorization": "Bearer tr", "Content-Type": "image/png"})
    assert r.text == "hello text" and r.headers["content-type"].startswith("text/plain")
    req = seen[0]
    assert str(req.url) == "https://llm.example/v1/chat/completions" and req.headers["authorization"] == "Bearer K"
    body = json.loads(req.content)
    assert body["model"] == "m1" and body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    c.post("/process", content=PNG, headers={"Authorization": "Bearer tl", "Content-Type": "image/jpeg"})
    assert str(seen[1].url).startswith("http://10.0.0.7:8000/") and "authorization" not in seen[1].headers  # the other profile, no credentials
    for h in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "tr"}):
        assert c.post("/process", content=PNG, headers={**h, "Content-Type": "image/png"}).status_code == 401
    assert len(seen) == 2


def test_caps_and_types():
    c, seen = client(max_body=10)
    h = {"Authorization": "Bearer tr", "Content-Type": "image/png"}
    assert c.post("/process", content=b"x" * 11, headers=h).status_code == 413
    assert c.post("/process", content=b"x", headers={**h, "Content-Type": "text/html"}).status_code == 415
    assert c.post("/process?url=http://evil", content=b"x", headers=h).status_code == 200 and "evil" not in str(seen[-1].url)  # never takes a URL
    assert not seen[:-1]


def test_protocol_errors_are_json_with_standard_codes_and_health_is_open():
    c, _ = client(max_body=10)
    h = {"Authorization": "Bearer tr", "Content-Type": "image/png"}
    for r in (c.post("/process", content=PNG, headers={"Content-Type": "image/png"}), c.post("/process", content=b"x" * 11, headers=h),
              c.post("/process", content=b"x", headers={**h, "Content-Type": "text/html"})):
        assert r.status_code in (401, 413, 415) and r.headers["content-type"].startswith("application/json") and r.json()["error"]
    assert c.get("/health").status_code == 200 and c.get("/process").status_code == 405 and c.post("/ocr", content=PNG, headers=h).status_code == 404


def test_upstream_failure_is_a_502():
    app = sidecar.make_app(PROFILES, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert TestClient(app).post("/process", content=PNG, headers={"Authorization": "Bearer tr", "Content-Type": "image/png"}).status_code == 502


def test_egress_is_pinned_to_the_profile_endpoints():
    ips = {"llm.example": "93.184.216.34"}
    eps = egress_rules.endpoints({**PROFILES, "g": {"base_url": "https://llm.example/v1", "auth": "google"}}, lambda h: ips.get(h, h))
    assert eps == [("93.184.216.34", 443), ("10.0.0.7", 8000), ("169.254.169.254", 80)]
    flat = [" ".join(c) for c in egress_rules.pinned_rules(7, ["127.0.0.11"], eps)]
    own = "-m owner --uid-owner 7"
    assert f"iptables -A OUTPUT {own} -d 10.0.0.7 -p tcp --dport 8000 -j ACCEPT" in flat  # a LAN profile is possible
    assert f"iptables -A OUTPUT {own} -d 127.0.0.11 -j ACCEPT" in flat  # Docker's resolver on any port (found on the live swarm)
    assert not any("127.0.0.11" in f and "--dport 53" in f for f in flat)
    assert flat.index(f"iptables -A OUTPUT {own} -d 169.254.169.254 -p tcp --dport 80 -j ACCEPT") < flat.index(f"iptables -A OUTPUT {own} -j REJECT")
    assert flat[-2:] == [f"iptables -A OUTPUT {own} -j REJECT", f"ip6tables -A OUTPUT {own} -j REJECT"]
    assert not any("169.254" in " ".join(c) for c in egress_rules.pinned_rules(7, [], egress_rules.endpoints(PROFILES, lambda h: h)))
