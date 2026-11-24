import asyncio
import base64
import os
import sys
import urllib.parse

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import upstream_auth as ua  # noqa: E402


def profile(**kw):
    return {"auth": "oauth2", "token_url": "https://idp.example/token", "client_id": "cid", "client_secret": "SECRET", "scope": "s", **kw}


class _Everything:
    def __contains__(self, _):
        return True


class Fake:
    """A fake IdP and model: `tokens` counts grants, the model accepts only the newest token unless `revoked`."""
    def __init__(self, status=200, expires_in=300):
        self.tokens, self.status, self.expires_in, self.valid, self.forms, self.reqs, self.revoked = 0, status, expires_in, None, [], [], set()

    def __call__(self, req: httpx.Request):
        if req.url.path == "/token":
            self.reqs.append(req)
            self.forms.append(dict(urllib.parse.parse_qsl(req.content.decode())))
            if self.status != 200:
                return httpx.Response(self.status, text="secret body SECRET")
            self.tokens += 1
            self.valid = f"t{self.tokens}"
            return httpx.Response(200, json={"access_token": self.valid, "expires_in": self.expires_in})
        ok = req.headers["authorization"] == f"Bearer {self.valid}" and self.valid not in self.revoked
        return httpx.Response(200 if ok else 401, json={})


def run(fake, fn):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as c:
            return await fn(c)
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def clean():
    ua._cache.clear()
    ua._locks.clear()


def test_grant_cache_and_concurrent_fetch_once():
    f = Fake()

    async def go(c):
        rs = await asyncio.gather(*[ua.post(c, profile(), "https://m.example/x") for _ in range(5)])
        assert all(r.status_code == 200 for r in rs)
        await ua.post(c, profile(), "https://m.example/x")
    run(f, go)
    assert f.tokens == 1
    assert f.forms[0] == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "SECRET", "scope": "s"}


def test_refresh_before_expiry():
    f = Fake(expires_in=20)  # under the 30 s margin: every call refreshes
    run(f, lambda c: ua.post(c, profile(), "https://m.example/x"))
    run(f, lambda c: ua.post(c, profile(), "https://m.example/x"))
    assert f.tokens == 2


def test_401_retries_once_with_a_fresh_token():
    f = Fake()

    async def go(c):
        await ua.post(c, profile(), "https://m.example/x")
        f.revoked.add("t1")  # the cached token is now rejected
        return await ua.post(c, profile(), "https://m.example/x")
    assert run(f, go).status_code == 200 and f.tokens == 2


def test_second_401_is_returned_not_retried_again():
    f = Fake()
    f.revoked = _Everything()
    assert run(f, lambda c: ua.post(c, profile(), "https://m.example/x")).status_code == 401 and f.tokens == 2


def test_token_failure_has_no_body_or_secret():
    f = Fake(status=500)
    with pytest.raises(ua.TokenError) as e:
        run(f, lambda c: ua.post(c, profile(), "https://m.example/x"))
    assert str(e.value) == "oauth2 token: 500" and "SECRET" not in str(e.value)


def test_key_isolation_and_basic_auth_and_env_secret(monkeypatch):
    f = Fake()
    monkeypatch.setenv("S_ENV", "FROMENV")

    async def go(c):
        await ua.headers(profile(scope="a"), c)
        await ua.headers(profile(scope="b"), c)
        await ua.headers(profile(token_auth="basic", client_secret_env="S_ENV", audience="aud"), c)
    run(f, go)
    assert f.tokens == 3
    basic = f.reqs[2]
    assert basic.headers["authorization"] == "Basic " + base64.b64encode(b"cid:FROMENV").decode()
    assert "client_secret" not in f.forms[2] and f.forms[2]["audience"] == "aud"


def test_validate():
    ua.validate({"a": {"auth": "none"}, "b": profile()})
    for bad in ({"auth": "oauth2"}, profile(client_secret=None), profile(client_secret_env="UNSET_X_Y"), profile(token_auth="x")):
        with pytest.raises(ValueError):
            ua.validate({"p": bad})
