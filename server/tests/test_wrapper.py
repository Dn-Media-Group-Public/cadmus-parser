"""The wrapper over `file://` stores (tests/harness.py), with stand-in scripts for the parser in the hostile-parser cases and the real worker for
the end-to-end ones. Not root here: slots run as the test's own uid (the root-only cases are skipped unless PARSE_CAPS_TEST is set)."""

import json
import os
import shutil
import time
import zipfile
import io

import httpx
import pytest

from cadmus_parser_server import wrapper
from harness import NO_METADATA, PROFILES, TOKEN, hostile, script


def drain(s, timeout=5):
    end = time.time() + timeout
    while "busy" in s.wrapper.pool.state.values() and time.time() < end:
        time.sleep(0.05)


# ---- API ---------------------------------------------------------------------------------------------------------

def test_auth_validation_and_error_shape(service):
    s = service()
    j = s.job({"a.md": b"# a"})
    assert j.post(token="wrong").status_code == 401 and j.post(token="wrong").json() == {"error": "unauthorized"}
    assert httpx.get(f"{s.url}/jobs").status_code == 405 and "error" in httpx.get(f"{s.url}/jobs").json()  # no GET of a job; errors are JSON
    assert httpx.get(f"{s.url}/nope").json()["error"] and httpx.get(f"{s.url}/version").json() == {"api": wrapper.API_VERSION}
    for body in ({}, {"input": {"url": j.input}}, {"input": {"url": ""}, "output": {"url": j.output}}, {"input": {"url": j.input, "options": 1}, "output": {"url": j.output}},
                 {**j.body(), "options": {"files": 1}}, {**j.body(), "features": {"ocr": 1}}):
        r = httpx.post(f"{s.url}/jobs", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 400 and r.json()["error"], body
    assert j.post(features={"nope": "x"}).status_code == 400
    r = j.post(features={"ocr": "unknown"})
    assert r.status_code == 400 and "profile" in r.json()["error"]
    assert httpx.post(f"{s.url}/jobs", content=b"{", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 400
    assert j.status() is None  # nothing was started


def test_202_carries_id_and_owner_and_status_is_written_first(service):
    s = service(parser_cmd=hostile("time.sleep(1)"), heartbeat=0.2)
    j = s.job({"a.md": b"# a"})
    r = j.post()
    assert r.status_code == 202 and r.json()["status"] == "running"
    assert r.json()["owner"].startswith("127.0.0.1:") and r.json()["id"]  # the local address the POST arrived on
    assert j.status()["status"] == "running" and j.status()["owner_replica"] == "wrapper"  # written before the 202
    assert j.wait()["status"] == "done" and j.index()["files"]["a.md"]["size"] == 3


def test_unusable_output_base_is_a_400_and_frees_the_slot(service):
    s = service(parser_cmd=hostile("pass"), pool_size=1)
    j = s.job({"a.md": b"# a"})
    shutil.rmtree(f"{j.root}/out")
    open(f"{j.root}/out", "w").write("a file where the output base should be")
    r = j.post()
    assert r.status_code == 400 and "NotADirectoryError" in r.json()["error"]
    assert set(s.wrapper.pool.state.values()) == {"free"}


def test_single_object_input_is_a_one_file_tree(service):
    s = service(parser_cmd=NO_METADATA)
    j = s.job({"a.md": b"# a"})
    r = httpx.post(f"{s.url}/jobs", json={"input": {"url": f"{j.input}/a.md"}, "output": {"url": j.output}}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 202 and j.wait()["status"] == "done" and list(j.index()["files"]) == ["a.md"]


# ---- the real worker, end to end -------------------------------------------------------------------------------------

def test_end_to_end_index_is_per_input_and_recursive(service):
    s = service(parser_cmd=NO_METADATA)
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("a/n.md", "# H\n[l](u)")
        z.writestr("in.zip", b"PK")
    page = b"<html><body><article><h1>Title</h1><p>" + b"Some long article text here. " * 20 + b"</p><h2>Part</h2></article></body></html>"
    j = s.job({"page.html": page, "d/b.zip": zb.getvalue(), "x.apk": b"PK\3\4"})
    assert j.post(options={"files": {"base_url": "http://h.test/", "ctypes": {"page.html": "text/html"}}}).status_code == 202
    assert j.wait()["status"] == "done"
    idx = j.index()
    f = idx["files"]
    assert set(f) == {"page.html", "d/b.zip", "x.apk"} and "notes" not in idx
    assert set(f["page.html"]["outputs"]) == {"parsed/page.html.md", "simplified/page.html.md", "terse/page.html.md"}
    assert f["page.html"]["content_type"] == "text/html" and f["page.html"]["size"] == len(page)
    assert f["page.html"]["outputs"]["parsed/page.html.md"]["content_type"] == "text/markdown"
    zf = f["d/b.zip"]["files"]
    assert set(zf) == {"a/n.md", "in.zip"} and zf["a/n.md"]["meta"] == {"text": True}
    assert "original/d/b.zip.d/a/n.md" in zf["a/n.md"]["outputs"] and "terse/d/b.zip.d/a/n.md" in zf["a/n.md"]["outputs"]
    assert zf["a/n.md"]["size"] == 10 and "files" not in zf["in.zip"]  # member sizes are claimed by the processor; nested archives are not unpacked again
    assert j.read("terse/page.html.md") == b"# Title\n## Part"
    assert not any(p.endswith(("status.json", "index.json")) for e in f.values() for p in e["outputs"])
    assert f["x.apk"]["outputs"] == {} and f["x.apk"]["size"] == 4  # an entry for every input, also when nothing came out
    # a second run over the same inputs (and what the first one unpacked) does not unpack the members again
    j2 = s.job({"d/b.zip": zb.getvalue(), "d/b.zip.d/in.zip": b"PK"})
    j2.post()
    j2.wait()
    f2 = j2.index()["files"]
    assert set(f2) == {"d/b.zip", "d/b.zip.d/in.zip"} and "files" not in f2["d/b.zip"] and "files" not in f2["d/b.zip.d/in.zip"]  # stored members are inputs of their own


def test_unhandled_files_and_failures_are_notes(service):
    s = service(parser_cmd=script("pass"))  # a parser that announces nothing
    j = s.job({"a.md": b"# a"})
    j.post()
    assert j.wait()["status"] == "done" and j.index()["files"]["a.md"] == {"size": 3, "outputs": {}}


def test_failed_status_has_the_sidecar_error_shape(service):
    s = service(parser_cmd=script("sys.exit(3)"))
    j = s.job({"a.md": b"# a"})
    j.post()
    st = j.wait()
    assert st["status"] == "failed" and st["error"] == "parser exited with 3" and "traceback" not in st and st["owner_replica"] == "wrapper" and "updated" in st
    assert set(j.index()["files"]) == {"a.md"}  # index.json is written before status.json, also on failure
    assert "error" not in j.index()


def test_unexpected_exceptions_carry_a_traceback_only_when_configured(service, monkeypatch):
    for tracebacks in (False, True):
        s = service(parser_cmd=script("pass"), tracebacks=tracebacks)
        monkeypatch.setattr(wrapper.Job, "pull", lambda self, indir, names: (_ for _ in ()).throw(OSError(28, "No space left on device")))
        j = s.job({"a.md": b"# a"})
        j.post()
        st = j.wait()
        assert st["status"] == "failed" and "No space" in st["error"] and ("Traceback" in st.get("traceback", "")) is tracebacks
        assert not os.listdir(s.cfg.scratch)
        monkeypatch.undo()


# ---- the parser is hostile ------------------------------------------------------------------------------------------

def rec(**r):
    return "os.write(1, " + repr(json.dumps(r).encode() + b"\n") + ")"


CASES = {
    "bad json": ("os.write(1, b'not json\\n')", "not JSON"),
    "not an object": ("os.write(1, b'[1]\\n')", "not an object"),
    "long record": ("os.write(1, b'{\"note\": \"' + b'x' * 5000 + b'\"}\\n')", "too long"),
    "dotdot": (rec(**{"in": ["a.md"], "path": "parsed/../../x", "content_type": "a/b"}), "bad path"),
    "absolute": (rec(**{"in": ["a.md"], "path": "/etc/passwd", "content_type": "a/b"}), "bad path"),
    "reserved top dir": (rec(**{"in": ["a.md"], "path": "index.json", "content_type": "a/b"}), "bad path"),
    "undeclared top dir": (rec(**{"in": ["a.md"], "path": "secrets/x", "content_type": "a/b"}), "bad path"),
    "top-level file": (rec(**{"in": ["a.md"], "path": "parsed", "content_type": "a/b"}), "bad path"),
    "no path": (rec(**{"in": ["a.md"], "content_type": "a/b"}), "bad path"),
    "unknown input": (rec(**{"in": ["ghost"], "note": "x"}), "unknown input"),
    "unknown input for an output": (rec(**{"in": ["ghost"], "path": "parsed/x", "content_type": "a/b"}), "unknown input"),
    "no input address": (rec(**{"path": "parsed/x", "content_type": "a/b"}), "input address"),
    "bad input address": (rec(**{"in": "a.md", "note": "x"}), "input address"),
    "empty input address": (rec(**{"in": [], "note": "x"}), "input address"),
    "info not an object": (rec(**{"in": ["a.md"], "info": 5}), "not an object"),
    "meta too big": (rec(**{"in": ["a.md"], "info": {"meta": {"k": "x" * 3000}}}), "meta"),
    "symlink": ("os.symlink('/etc/passwd', jobdir + '/out/parsed/l'); " + rec(**{"in": ["a.md"], "path": "parsed/l", "content_type": "a/b"}), None),
    "symlinked dir": ("os.symlink('/etc', jobdir + '/out/parsed/d'); " + rec(**{"in": ["a.md"], "path": "parsed/d/passwd", "content_type": "a/b"}), None),
    "directory": ("os.mkdir(jobdir + '/out/parsed/dir'); " + rec(**{"in": ["a.md"], "path": "parsed/dir", "content_type": "a/b"}), "not a regular"),
    "fifo": ("os.mkfifo(jobdir + '/out/parsed/f'); " + rec(**{"in": ["a.md"], "path": "parsed/f", "content_type": "a/b"}), "not a regular"),
    "missing file": (rec(**{"in": ["a.md"], "path": "parsed/none", "content_type": "a/b"}), None),
    "nonzero exit": ("sys.exit(3)", "exited with 3"),
    "killed": ("os.kill(os.getpid(), 9)", "exited with -9"),
    "truncated record": ("os.write(1, b'{\"path\": \"parsed/x\", \"cont')", "truncated"),
}


@pytest.mark.parametrize("name", CASES)
def test_hostile_parser(service, name):
    code, want = CASES[name]
    s = service(parser_cmd=hostile(code))
    j = s.job({"a.md": b"# a"})
    assert j.post().status_code == 202
    st = j.wait()
    assert st["status"] == "failed", st
    if want:
        assert want in st["error"], st
    assert set(j.index()["files"]) == {"a.md"} and j.outputs() == []  # nothing hostile reached the store
    drain(s)
    assert set(s.wrapper.pool.state.values()) == {"free"} and not os.listdir(s.cfg.scratch)  # slot wiped and freed


def test_size_comes_from_fstat_and_artefacts_are_deleted_after_upload(service):
    s = service(parser_cmd=hostile(
        "open(jobdir + '/out/parsed/a.md', 'wb').write(b'hello')\n"
        + rec(**{"in": ["a.md"], "path": "parsed/a.md", "content_type": "text/markdown", "size": 999999, "meta": {"k": 1}}) + "\n"
        "time.sleep(0.5)\nprint(os.path.exists(jobdir + '/out/parsed/a.md'), file=sys.stderr)\n"
        + rec(**{"in": ["a.md"], "info": {"content_type": "text/x", "meta": {"text": True}, "size": 7}}) + "\n"
        + rec(**{"in": ["a.md", "m/x.txt"], "info": {"size": 12, "content_type": "text/plain"}}) + "\n"
        + rec(**{"in": ["a.md", "m/x.txt"], "note": "member note"}) + "\n"
        + rec(**{"in": ["a.md"], "note": "one"}) + "\n" + rec(**{"in": ["a.md"], "note": "two"}) + "\n" + rec(note="hi")))
    j = s.job({"a.md": b"# a"})
    j.post()
    assert j.wait()["status"] == "done"
    e = j.index()["files"]["a.md"]
    assert e["outputs"]["parsed/a.md"] == {"size": 5, "content_type": "text/markdown", "meta": {"k": 1}}
    assert e["size"] == 3 and e["content_type"] == "text/x" and e["meta"] == {"text": True} and e["notes"] == "one\ntwo"  # the input's size is ours, the claim is ignored
    assert e["files"]["m/x.txt"] == {"size": 12, "content_type": "text/plain", "outputs": {}, "notes": "member note"}
    assert j.index()["notes"] == "hi" and j.read("parsed/a.md") == b"hello"


def test_an_output_path_is_accepted_once_per_job(service):
    s = service(parser_cmd=hostile(
        "open(jobdir + '/out/parsed/a', 'wb').write(b'1')\n" + rec(**{"in": ["a.md"], "path": "parsed/a", "content_type": "a/b"}) + "\n"
        + rec(**{"in": ["b.md"], "path": "parsed/a", "content_type": "a/b"})))
    j = s.job({"a.md": b"# a", "b.md": b"# b"})
    j.post()
    assert j.wait()["status"] == "done"
    f = j.index()["files"]
    assert "parsed/a" in f["a.md"]["outputs"] and f["b.md"]["outputs"] == {} and "written twice" in f["b.md"]["notes"]


def test_depth_and_entry_caps_add_a_note_and_do_not_fail(service):
    deep = ["a.md"] + [f"d{i}" for i in range(8)]
    s = service(parser_cmd=hostile(rec(**{"in": deep, "note": "x"}) + "\n" + "".join(rec(**{"in": ["a.md", f"m{i}"], "note": "n"}) + "\n" for i in range(5))), max_entries=4)
    j = s.job({"a.md": b"# a"})
    j.post()
    assert j.wait()["status"] == "done"
    idx = j.index()
    assert sorted(idx["files"]["a.md"]["files"]) == ["m0", "m1", "m2"] and "nested more than 8" in idx["notes"] and "more than 4 entries" in idx["notes"]


def test_caps_and_deadline(service):
    s = service(parser_cmd=hostile("for i in range(10): os.write(1, b'{\"note\": \"n\"}\\n')"), max_records=3)
    j = s.job({"a.md": b"# a"})
    j.post()
    assert "too many records" in j.wait()["error"]
    s = service(parser_cmd=hostile("open(jobdir + '/out/parsed/a', 'wb').write(b'x' * 100)\n" + rec(**{"in": ["a.md"], "path": "parsed/a", "content_type": "a/b"})),
                limits={"default": {"output_mib": 10 / (1 << 20)}})  # 10 bytes
    j = s.job({"a.md": b"# a"})
    j.post()
    assert "output too big" in j.wait()["error"]
    s = service(parser_cmd=hostile("time.sleep(60)"), limits={"default": {"deadline_s": 1}})
    j = s.job({"a.md": b"# a"})
    j.post()
    assert "timed out after 1 s" in j.wait()["error"]


def test_parser_environment_has_no_secrets_and_only_the_requested_profile(service, monkeypatch):
    monkeypatch.setenv("WRAPPER_TOKEN", "leak-me")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    s = service(parser_cmd=hostile("os.write(1, json.dumps({'note': json.dumps(dict(os.environ))}).encode() + b'\\n')"))
    j = s.job({"a.md": b"# a"})
    j.post(features={"ocr": "lan"})
    assert j.wait()["status"] == "done"
    env = json.loads(j.index()["notes"])
    assert env["OCR_URL"] == "http://ocr-lan" and env["OCR_TOKEN"] == "tok-lan" and "tok-remote" not in json.dumps(env)
    assert "leak-me" not in json.dumps(env) and "KEY" not in "".join(env) and env["PYTHONUNBUFFERED"] == "1"
    j = s.job({"a.md": b"# a"})
    assert j.post().status_code == 202  # no feature: no OCR variables at all
    j.wait()
    assert not [k for k in json.loads(j.index()["notes"]) if k.startswith("OCR")]
    # the operator's limits reach the parser (the only part of the wrapper's own environment that does), a lookalike secret does not
    monkeypatch.setenv("MAX_PARSE_BYTES", "12345")
    monkeypatch.setenv("METADATA_TIMEOUT", "7")
    for n in ("FILES", "IMAGE", "VIDEO", "SOUND"):
        monkeypatch.setenv(f"MAX_PARSE_BYTES_{n}", n)
    monkeypatch.setenv("MAX_ARCHIVE_BYTESX", "leak-me")
    j = s.job({"a.md": b"# a"})
    assert j.post().status_code == 202  # the slot of the job just seen done is already free
    j.wait()
    env = json.loads(j.index()["notes"])
    assert env["MAX_PARSE_BYTES"] == "12345" and env["METADATA_TIMEOUT"] == "7" and all(env[f"MAX_PARSE_BYTES_{n}"] == n for n in ("FILES", "IMAGE", "VIDEO", "SOUND")) and "MAX_ARCHIVE_BYTESX" not in env


def test_duplicate_post_busy_pool_and_stale_heartbeat(service):
    s = service(parser_cmd=hostile("time.sleep(2)"), pool_size=1, heartbeat=0.2)
    j = s.job({"a.md": b"# a"})
    assert j.post().status_code == 202
    assert j.post().status_code == 409 and j.post().json() == {"error": "job is running"}  # same output base, running with a fresh heartbeat
    j2 = s.job({"a.md": b"# a"})
    assert j2.post().status_code == 503  # the only uid is busy
    assert j.status()["status"] == "running"
    assert j.wait()["status"] == "done"
    assert j2.post().status_code == 202  # a freed slot takes the next job
    j2.wait()
    # another replica (no memory) reads status.json from the store; an old heartbeat means the owner is lost
    j3 = s.job({})
    open(f"{j3.root}/out/status.json", "w").write(json.dumps({"status": "running", "owner_replica": "x", "updated": time.time() - 60}))
    assert j3.post().status_code == 202  # restarted
    j3.wait()
    open(f"{j3.root}/out/status.json", "w").write(json.dumps({"status": "running", "owner_replica": "x", "updated": time.time()}))
    s.wrapper.memory.clear()
    assert j3.post().status_code == 409


def test_identical_output_base_only(service):
    """Nested bases are not detected: the job's identity is its output url (a trailing slash does not make a new one)."""
    s = service(parser_cmd=hostile("time.sleep(1)"), pool_size=2, heartbeat=0.2)
    j = s.job({"a.md": b"# a"})
    assert j.post().status_code == 202
    again = httpx.post(f"{s.url}/jobs", json={"input": {"url": j.input}, "output": {"url": j.output + "/"}}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert again.status_code == 409
    nested = httpx.post(f"{s.url}/jobs", json={"input": {"url": j.input}, "output": {"url": j.output + "/sub"}}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert nested.status_code == 202
    j.wait()


# ---- credentials ---------------------------------------------------------------------------------------------------

def seen_options(s, monkeypatch):
    calls = []
    real = s.wrapper.fs

    def fs(url, creds):
        calls.append((url, dict(creds)))
        return real(url, {k: v for k, v in creds.items() if not k.startswith("tok")})
    monkeypatch.setattr(s.wrapper, "fs", fs)
    return calls


def test_each_side_has_its_own_credential_and_the_blob_options_are_merged_under(service, monkeypatch):
    s = service(parser_cmd=hostile("pass"), blob_options={"auto_mkdir": True})
    calls = seen_options(s, monkeypatch)
    j = s.job({"a.md": b"# a"})
    r = httpx.post(f"{s.url}/jobs", json={"input": {"url": j.input, "options": {"tok": "in-1"}}, "output": {"url": j.output, "options": {"tok": "out-1"}}},
                   headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 202 and j.wait()["status"] == "done"
    assert {(u == j.input, o["tok"]) for u, o in calls} == {(True, "in-1"), (False, "out-1")}  # a credential never crosses to the other side


def test_renew_replaces_credentials_per_transfer_and_answers_404_410_400(service, monkeypatch):
    s = service(parser_cmd=hostile("time.sleep(1.5)"), heartbeat=0.2)
    calls = seen_options(s, monkeypatch)
    j = s.job({"a.md": b"# a"})
    r = httpx.post(f"{s.url}/jobs", json={"input": {"url": j.input, "options": {"tok": "in-1"}}, "output": {"url": j.output, "options": {"tok": "out-1"}}},
                   headers={"Authorization": f"Bearer {TOKEN}"})
    jid = r.json()["id"]
    head = {"Authorization": f"Bearer {TOKEN}"}

    def renew(**body):
        return httpx.post(f"{s.url}/jobs/renew", json=body, headers=head)

    assert renew(id=jid, options={j.output: {"tok": "out-2"}, j.input: {"tok": "in-2"}}).status_code == 200
    assert renew(id=jid, options={"s3://elsewhere/x": {"tok": "x"}}).status_code == 400  # a key matching neither url
    assert renew(id="unknown", options={}).status_code == 404
    assert httpx.post(f"{s.url}/jobs/renew", json={"id": jid, "options": {}}).status_code == 401
    assert renew(id=jid).status_code == 400
    assert j.wait()["status"] == "done"
    deadline = time.time() + 5
    while renew(id=jid, options={}).status_code != 410 and time.time() < deadline:
        time.sleep(0.05)
    assert renew(id=jid, options={}).status_code == 410 and renew(id=jid, options={}).json()["error"]
    # the status writes of the heartbeat and the final index/status used the renewed output credential
    assert [o["tok"] for u, o in calls if u == j.output][-1] == "out-2"


def test_a_credential_error_when_listing_fails_the_job_clearly(service, monkeypatch):
    s = service(parser_cmd=hostile("pass"))
    j = s.job({"a.md": b"# a"})

    class NoList:
        def find(self, *a, **k):
            raise PermissionError("denied")
    real = s.wrapper.fs
    monkeypatch.setattr(s.wrapper, "fs", lambda url, creds: (NoList(), "/x") if url == j.input else real(url, creds))
    j.post()
    st = j.wait()
    assert st["status"] == "failed" and "list permission" in st["error"]


# ---- limits ----------------------------------------------------------------------------------------------------------

def test_input_size_checks_before_and_after_the_pull(service):
    s = service(parser_cmd=hostile("pass"), limits={"default": {"input_mib": 1}, "video": {"input_mib": 3}})
    j = s.job({"a.md": b"x" * (4 << 20)})
    j.post()
    st = j.wait()
    assert st["status"] == "failed" and "input too big (4 MiB > 3 MiB)" in st["error"]  # the operator's ceiling, from the listing, before any pull
    assert not os.listdir(f"{j.root}/out") or j.outputs() == []
    j = s.job({"a.md": b"x" * (2 << 20)})  # under the ceiling, over its own class
    j.post()
    assert "input too big for its class (2 MiB > 1 MiB)" in j.wait()["error"]
    j = s.job({"a.mp4": b"x" * (2 << 20)})
    j.post()
    assert j.wait()["status"] == "done"


def test_limits_follow_the_classes_present(service):
    code = ("import resource\nos.write(1, json.dumps({'note': json.dumps({'as': resource.getrlimit(resource.RLIMIT_AS)[0] >> 20, "
            "'fsize': resource.getrlimit(resource.RLIMIT_FSIZE)[0] >> 20})}).encode() + b'\\n')")
    limit = {"default": {"ram_mib": 600, "fsize_mib": 7}, "video": {"ram_mib": 900}, "image": {"ram_mib": 700}}
    s = service(parser_cmd=hostile(code), limits=limit)
    for files, want in (({"a.pdf": b"%PDF"}, {"as": 600, "fsize": 7}), ({"a.png": b"x"}, {"as": 700, "fsize": 7}),
                        ({"a.png": b"x", "b.mp4": b"x"}, {"as": 900, "fsize": 7}), ({"x": b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'}, {"as": 700, "fsize": 7})):
        j = s.job(files)
        j.post()
        assert j.wait()["status"] == "done"
        assert json.loads(j.index()["notes"]) == want, files
        drain(s)
    s = service(parser_cmd=hostile(code), limits={"default": {"ram_mib": 0}, "image": {"ram_mib": 700}})  # an explicit 0 is unlimited, and the largest
    j = s.job({"a.png": b"x", "b.pdf": b"x"})
    j.post()
    j.wait()
    assert json.loads(j.index()["notes"])["as"] == -1  # RLIM_INFINITY


def test_a_processor_cycle_fails_the_job_with_the_reason(service, monkeypatch):
    """The real worker with two installed processors that want each other first: the error is in status.json."""
    import pathlib
    import tempfile
    tmp_path = pathlib.Path(tempfile.mkdtemp())
    tmp_path.chmod(0o755)  # the pool uid must be able to import it
    mod = tmp_path / "cycmod.py"
    mod.write_text("class A:\n name='a'; after=['b']; top_dirs=('parsed',)\n def accepts(s,f): return True\n def process(s,k,c): pass\n"
                   "class B:\n name='b'; after=['a']; top_dirs=('parsed',)\n def accepts(s,f): return True\n def process(s,k,c): pass\n")
    mod.chmod(0o644)
    s = service(parser_cmd=script(f"sys.path.insert(0, {str(tmp_path)!r})\nimport cycmod\nfrom cadmus_parser_server import processors, worker\n"
                                  "processors.load = lambda: [cycmod.A(), cycmod.B()]\nworker.main(jobdir)"))
    j = s.job({"a.md": b"# a"})
    j.post()
    st = j.wait()
    assert st["status"] == "failed" and "processor cycle: a, b" in st["error"] and "exited with 2" in st["error"]


def test_discovery_reports_what_the_installed_processors_declare():
    found = wrapper.discover()
    assert {"parsed", "terse", "metadata", "original", "simplified"} <= found  # the `files` processor, when its extra is installed


# ---- as the container runs it -------------------------------------------------------------------------------------------

root_only = pytest.mark.skipif(not os.environ.get("PARSE_CAPS_TEST"), reason="run in the parser container as the wrapper uid with its capabilities")


@root_only
def test_parser_uid_holds_nothing_and_cannot_read_the_wrapper(service):
    """With real pool uids (the container run, as uid 900 with CAP_SETUID/SETGID/CHOWN/KILL): who the parser is and what it can touch."""
    s = service(parser_cmd=hostile(
        "ppid = os.getppid()\n"
        "def can(f):\n"
        "    try:\n        f(); return True\n    except OSError:\n        return False\n"
        "st = open('/proc/self/status').read()\n"
        "info = {'uid': os.getuid(), 'euid': os.geteuid(), 'gid': os.getgid(), 'groups': os.getgroups(),\n"
        "        'capeff': [l for l in st.splitlines() if l.startswith('CapEff')][0].split()[1], 'nnp': [l for l in st.splitlines() if l.startswith('NoNewPrivs')][0].split()[1],\n"
        "        'environ': can(lambda: open(f'/proc/{ppid}/environ').read()), 'setuid': can(lambda: os.setuid(0)),\n"
        "        'other_slot': can(lambda: os.listdir(os.path.dirname(jobdir) + '/slot-1')),\n"
        "        'kill_wrapper': can(lambda: os.kill(ppid, 0)) and can(lambda: os.kill(ppid, 15)),\n"
        "        'write_etc': can(lambda: open('/etc/x', 'w')), 'write_venv': can(lambda: open('/venv/x', 'w')),\n"
        "        'nofile': __import__('resource').getrlimit(7)[0], 'nproc': __import__('resource').getrlimit(6)[0], 'as': __import__('resource').getrlimit(9)[0]}\n"
        "os.write(1, json.dumps({'note': json.dumps(info)}).encode() + b'\\n')"), pool_size=2, limits={"default": {"ram_mib": 2048}})
    j = s.job({"a.md": b"# a"})
    j.post()
    assert j.wait()["status"] == "done"
    info = json.loads(j.index()["notes"])
    assert info["uid"] == info["euid"] == 20000 and info["gid"] == 20000 and info["groups"] == []
    assert info["capeff"] == "0000000000000000" and info["nnp"] == "1" and not info["setuid"]
    assert not info["environ"] and not info["other_slot"] and not info["write_etc"] and not info["write_venv"]  # the wrapper's environ (tokens), files and the image are out of reach
    assert info["nofile"] == 1024 and info["nproc"] == 1024 and info["as"] == 2 << 30
    assert os.listdir(s.cfg.scratch) == [] and s.wrapper.pool.state == {0: "free", 1: "free"}  # wiped and freed


@root_only
def test_a_parser_that_leaves_files_and_processes_is_cleaned_up(service):
    s = service(parser_cmd=hostile(
        "open('/tmp/leftover', 'w').write('x')\nos.mkdir('/tmp/leftdir'); os.chmod('/tmp/leftdir', 0)\n"
        "if os.fork() == 0:\n    os.setsid(); time.sleep(120)\n"
        "os.write(1, b'{\"note\": \"ok\"}\\n')"), pool_size=1)
    j = s.job({"a.md": b"# a"})
    j.post()
    assert j.wait()["status"] == "done"
    drain(s)
    assert s.wrapper.pool.state == {0: "free"} and not os.path.exists("/tmp/leftover") and not os.path.exists("/tmp/leftdir")
    assert wrapper.owned_by(20000, ["/tmp", s.cfg.scratch]) is None

    def alive(p):  # a zombie is dead (pytest is pid 1 in the container run and does not reap)
        try:
            return os.stat(f"/proc/{p}").st_uid == 20000 and open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False
    assert not [p for p in os.listdir("/proc") if p.isdigit() and alive(p)]  # the setsid'ed child is dead too


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe not installed (the parser image has it)")
def test_hostile_media_and_raster_files_open_no_connection(service):
    """An .m3u8 that makes ffprobe fetch URLs and a .vrt pointing at /vsicurl/: the real worker, the real extractors, nothing connects."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    s = service()
    j = s.job({
        "a.m3u8": f"#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1:{port}/seg.ts\n".encode(),
        "b.vrt": f'<VRTDataset rasterXSize="1" rasterYSize="1"><VRTRasterBand dataType="Byte" band="1"><SimpleSource><SourceFilename relativeToVRT="0">/vsicurl/http://127.0.0.1:{port}/x.tif</SourceFilename></SimpleSource></VRTRasterBand></VRTDataset>'.encode(),
        "c.mp4": b"\0\0\0\x18ftypmp42\0\0\0\0mp42isom"})
    j.post()
    assert j.wait()["status"] == "done"
    try:
        conn, _ = srv.accept()
        conn.close()
        raise AssertionError("a parser opened a connection to a URL from an untrusted file")
    except socket.timeout:
        pass


def test_usage_is_registered_collected_and_written_to_status(service, monkeypatch):
    calls = []

    def fake(p, method, path, body=None):
        calls.append((p["url"], method, path, body))
        if method == "POST":
            return {"token": "job-tok"}
        if p["url"] == "http://ocr-lan":
            return {"calls": [{"t": 2, "model": "m"}, {"t": 1, "model": "m"}, {"t": 3, "model": "m"}], "truncated": False}
        raise OSError("down")
    monkeypatch.setattr(wrapper, "sidecar_call", fake)
    profiles = {"ocr": {"lan": {"url": "http://ocr-lan", "token": "tok-lan", "admin_token": "adm"}, "remote": {"url": "http://ocr-remote", "token": "tok-remote"}}}
    s = service(profiles=profiles, max_usage_calls=2,
                parser_cmd=hostile("os.write(1, json.dumps({'note': os.environ['OCR_TOKEN']}).encode() + b'\\n')"))
    j = s.job({"a.md": b"# a"})
    j.post(features={"ocr": "lan"})
    st = j.wait()
    assert j.index()["notes"] == "job-tok"  # the parser holds the job token, not the profile token
    assert calls[0][1:3] == ("POST", "/admin/jobs") and calls[0][3]["job"] and calls[0][3]["profile"] == "lan"
    assert st["usage"] == {"calls": [{"t": 1, "model": "m"}, {"t": 2, "model": "m"}], "truncated": True}
    j = s.job({"a.md": b"# a"})  # a profile without admin_token: unchanged, no usage field
    j.post(features={"ocr": "remote"})
    assert "usage" not in j.wait() and j.index()["notes"] == "tok-remote"
    monkeypatch.setattr(wrapper, "sidecar_call", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    j = s.job({"a.md": b"# a"})  # sidecar down: the job still succeeds and says so
    j.post(features={"ocr": "lan"})
    st = j.wait()
    assert st["status"] == "done" and st["usage"]["error"] == "ocr: OSError" and st["usage"]["calls"] == []


def test_two_features_on_one_sidecar_have_separate_usage_records(service, monkeypatch):
    records = {}

    def fake(p, method, path, body=None):  # a stand-in sidecar: one record per registered id, dropped when read
        if method == "POST":
            records[body["job"]] = [{"t": len(records), "profile": body["profile"]}]
            return {"token": "tok-" + body["profile"]}
        return {"calls": records.pop(path.split("/")[3]), "truncated": False}  # a second read of the same id would raise KeyError
    monkeypatch.setattr(wrapper, "sidecar_call", fake)
    side = {"url": "http://asr", "token": "t", "admin_token": "adm"}
    s = service(profiles={"asr": {"p1": side}, "asr-fallback": {"p2": side}}, parser_cmd=hostile("pass"))
    j = s.job({"a.md": b"# a"})
    j.post(features={"asr": "p1", "asr-fallback": "p2"})
    st = j.wait()
    assert st["usage"] == {"calls": [{"t": 0, "profile": "p1"}, {"t": 1, "profile": "p2"}], "truncated": False} and not records
