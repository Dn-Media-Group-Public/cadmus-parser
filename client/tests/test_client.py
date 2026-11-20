"""The client against a real wrapper (cadmus-parser-server, `file://` stores, stand-in parsers): post, wait with the heartbeat rule, index, renew, renew_loop."""

import json
import os
import socket
import sys
import tempfile
import threading
import time

import httpx
import pytest

pytest.importorskip("cadmus_parser_server")
import uvicorn

from cadmus_parser import Client, ParseError, RenewLoop, renew_loop
from cadmus_parser_server import wrapper

TOKEN = "t0k"
TOP = {"parsed"}


def script(code):
    return [sys.executable, "-I", "-u", "-c", "import os, sys, json, time\njobdir = sys.argv[1]\n" + code]


class Svc:
    def __init__(self, **cfg):
        cfg.setdefault("blob_options", {"auto_mkdir": True})
        self.cfg = wrapper.Config(TOKEN, scratch=tempfile.mkdtemp(), pool_size=2, top_dirs=TOP, **cfg)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.server = uvicorn.Server(uvicorn.Config(wrapper.make_app(self.cfg), host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.05)
        self.client = Client(f"http://127.0.0.1:{port}", TOKEN)
        self.root = tempfile.mkdtemp()
        os.makedirs(f"{self.root}/in")
        open(f"{self.root}/in/a.md", "w").write("# a")
        self.input, self.output = f"file://{self.root}/in", f"file://{self.root}/out"


@pytest.fixture
def svc():
    made = []

    def make(code="pass", **cfg):
        made.append(Svc(parser_cmd=script("os.makedirs(jobdir + '/out/parsed', exist_ok=True)\n" + code), **cfg))
        return made[-1]
    yield make
    for s in made:
        s.server.should_exit = True


def test_post_wait_index_and_errors(svc):
    s = svc("os.write(1, json.dumps({'in': ['a.md'], 'note': 'hi'}).encode() + b'\\n')")
    assert s.client.status(s.output) == {"status": "not_started"}
    started = s.client.post(s.input, s.output)
    assert started.id and started.owner.startswith("127.0.0.1:")
    st = s.client.wait(s.output)
    assert st["status"] == "done" and s.client.index(s.output)["files"]["a.md"]["notes"] == "hi"
    assert s.client.index(f"file://{s.root}/none") == {"files": {}}
    with pytest.raises(ParseError) as e:
        Client(s.client.url, "wrong").post(s.input, s.output)
    assert e.value.status == 401 and "unauthorized" in e.value.message
    with pytest.raises(ParseError) as e:
        s.client.post(s.input, s.output, features={"nope": "x"})
    assert e.value.status == 400
    with pytest.raises(ParseError) as e:
        Client("http://127.0.0.1:9", TOKEN).post(s.input, s.output)
    assert e.value.status == 0 and "unavailable" in e.value.message


def test_failure_has_the_error_and_the_heartbeat_rule(svc):
    s = svc("sys.exit(3)")
    s.client.post(s.input, s.output)
    assert s.client.wait(s.output)["error"] == "parser exited with 3"
    os.makedirs(f"{s.root}/lost", exist_ok=True)
    open(f"{s.root}/lost/status.json", "w").write(json.dumps({"status": "running", "owner_replica": "x", "updated": time.time() - 60}))
    assert s.client.status(f"file://{s.root}/lost") == {"status": "failed", "error": "wrapper lost"}
    assert s.client.wait(f"file://{s.root}/lost", poll_timeout=5)["error"] == "wrapper lost"
    assert s.client.wait(f"file://{s.root}/never", poll_timeout=0.3) == {"status": "failed", "error": "timed out waiting for the parser"}


def test_a_503_is_retried_once(svc, monkeypatch):
    s = svc()
    calls, real = [], httpx.post

    def post(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "no free parser"})
        return real(url, **kw)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert s.client.post(s.input, s.output).id and len(calls) == 2


def test_renew_codes(svc):
    s = svc("time.sleep(1)")
    st = s.client.post(s.input, s.output)
    assert s.client.renew(st.owner, st.id, {s.output: {}}) == 200
    assert s.client.renew(st.owner, "nope", {}) == 404
    s.client.wait(s.output)
    deadline = time.time() + 5
    while s.client.renew(st.owner, st.id, {}) != 410 and time.time() < deadline:
        time.sleep(0.05)
    assert s.client.renew(st.owner, st.id, {}) == 410


class FakeHandle(dict):
    n = 0

    def __init__(self, grants):
        super().__init__({u: {"n": FakeHandle.n} for u, _ in grants})
        FakeHandle.n += 1
        self.revoked = False

    def revoke(self):
        self.revoked = True


def test_renew_loop_pushes_fresh_credentials_and_revokes_old_ones(svc, monkeypatch):
    from cadmus_parser import renew
    s = svc("time.sleep(2)", heartbeat=0.2)
    sent, minted = [], []
    monkeypatch.setattr(renew, "mint", lambda grants, lifetime, options: minted.append(FakeHandle(grants)) or minted[-1])
    real = s.client.renew
    s.client.renew = lambda owner, jid, creds, rekey=None: (sent.append(dict(creds)), real(owner, jid, creds, rekey))[1]
    grants = [(s.input, "read"), (s.output, "write")]
    first = FakeHandle(grants)
    st = s.client.post(s.input, s.output, first)
    loop = renew_loop(s.client, grants, st.id, st.owner, first, interval=0.2)
    time.sleep(1.0)
    assert len(sent) >= 3 and not loop.lost and not loop.finished
    assert first.revoked and not minted[-1].revoked and not minted[-2].revoked  # the last two stay: a transfer may still use the one before
    assert sent[-1][s.output] == {"n": minted[-1][s.output]["n"]}
    loop.stop()
    assert all(h.revoked for h in minted) and loop.handles == []
    s.client.wait(s.output)


def test_renew_loop_stops_when_the_job_is_finished_or_lost(svc, monkeypatch):
    from cadmus_parser import renew
    s = svc()
    monkeypatch.setattr(renew, "mint", lambda grants, lifetime, options: FakeHandle(grants))
    st = s.client.post(s.input, s.output)
    s.client.wait(s.output)
    time.sleep(0.2)
    loop = RenewLoop(s.client, [(s.output, "write")], st.id, st.owner, interval=0.05)
    deadline = time.time() + 5
    while not loop.finished and time.time() < deadline:
        time.sleep(0.05)
    assert loop.finished and not loop.lost
    loop.stop()
    gone = RenewLoop(s.client, [(s.output, "write")], st.id, "127.0.0.1:9", interval=0.05, retries=1, retry_delay=0.01)
    deadline = time.time() + 5
    while not gone.lost and time.time() < deadline:
        time.sleep(0.05)
    assert gone.lost  # a persistent connection error means the replica is gone
    gone.stop()


def test_a_grant_wider_than_the_posted_urls_is_rekeyed_and_the_reader_follows_the_current_credential(svc, monkeypatch):
    from cadmus_parser import renew
    s = svc("time.sleep(1)", heartbeat=0.2)
    wide_in, wide_out = f"file://{s.root}", s.output  # a project-wide read grant, a job-wide write grant
    grants = [(wide_in, "read"), (wide_out, "write")]
    rekey = {wide_in: s.input}
    seen, minted = [], []
    real = s.client.renew
    s.client.renew = lambda owner, jid, creds, rk=None: (seen.append(rk), real(owner, jid, creds, rk))[1]
    monkeypatch.setattr(renew, "mint", lambda g, l, o: minted.append(FakeHandle(g)) or minted[-1])
    first = FakeHandle(grants)
    st = s.client.post(s.input, s.output, input_options=first[wide_in], output_options=first[wide_out])
    tracked = []
    loop = renew_loop(s.client, grants, st.id, st.owner, first, interval=0.1, rekey=rekey, on_mint=tracked.append)
    time.sleep(0.6)
    assert seen and all(r == rekey for r in seen) and tracked and tracked[-1] is loop.current  # renewals matched the posted input url, 200 each time
    assert not loop.lost and not loop.finished
    assert s.client.wait(s.output, lambda: loop.current[wide_out])["status"] == "done"  # callable options: the newest credential
    loop.stop()


def test_run_is_the_whole_job_and_handles_wide_grants(svc, monkeypatch):
    from cadmus_parser import client as cl, minting, renew
    s = svc("os.write(1, json.dumps({'in': ['a.md'], 'note': 'hi'}).encode() + b'\\n')")
    made = []
    fake = lambda grants, lifetime, options: made.append(FakeHandle(grants)) or made[-1]  # noqa: E731
    monkeypatch.setattr(minting, "mint", fake)
    monkeypatch.setattr(renew, "mint", fake)
    status, index = s.client.run(s.input, s.output)
    assert status["status"] == "done" and index["files"]["a.md"]["notes"] == "hi" and made and all(h.revoked for h in made)  # credentials revoked at the end
    made.clear()
    seen = []
    on_mint = seen.append
    s2 = svc("time.sleep(0.5)", heartbeat=0.2)
    os.makedirs(f"{s2.root}/in", exist_ok=True)
    status, index = s2.client.run(s2.input, s2.output, [(f"file://{s2.root}", "write")], renew_every=0.1, on_mint=on_mint)  # one grant above both posted urls
    assert status["status"] == "done" and len(seen) >= 2 and all(h.revoked for h in seen)
    with pytest.raises(ValueError):
        cl._grant_for({"s3://a/b": {}}, "s3://other/x")
    assert cl._grant_for({"s3://a/b": {}, "s3://a/b/c": {}}, "s3://a/b/c/d") == "s3://a/b/c" and cl._grant_for({"s3://a/b": {}}, "s3://a/b") == "s3://a/b"
    with pytest.raises(ParseError):
        Client(s.client.url, "wrong").run(s.input, s.output)
    assert all(h.revoked for h in made)


def test_renew_rekey_may_name_both_posted_urls(svc):
    s = svc("time.sleep(1)", heartbeat=0.2)
    seen = {}
    st = s.client.post(s.input, s.output)
    import httpx as hx
    real = hx.post
    def spy(url, **kw):
        if url.endswith("/jobs/renew"):
            seen.update(kw["json"]["options"])
        return real(url, **kw)
    hx.post = spy
    try:
        assert s.client.renew(st.owner, st.id, {"g": {"k": 1}}, {"g": [s.input, s.output]}) == 200
    finally:
        hx.post = real
    assert seen == {s.input: {"k": 1}, s.output: {"k": 1}}
    seen.clear()
    assert s.client.renew(st.owner, st.id, {"g": {"k": 1}, "unused": {"k": 2}}, {"g": [s.output], "unused": []}) in (200, 410)  # an uncovered grant is dropped, not a 400
    s.client.wait(s.output)
