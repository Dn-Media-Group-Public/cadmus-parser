"""Upstream authentication of the model sidecars (ocr, asr, caption), one copy for all three. A profile's `auth` is `none`, `bearer` (`api_key`),
`google` (an ID token for `audience`) or `oauth2`: the client-credentials grant (`token_url`, `client_id`, `client_secret` or `client_secret_env`,
`scope?`, `audience?`, `token_auth?` = `post` (default, credentials in the body) | `basic`). OAuth2 tokens are cached in memory per
(token_url, client_id, scope, audience) and refreshed when fewer than max(30 s, 10 % of the lifetime) remain; nothing is logged.
The sidecars call `post` (one retry with a fresh token on 401/403) and `validate` at start."""

import asyncio
import os
import time

import httpx


class TokenError(Exception):
    """The token endpoint failed. The message is safe to return to the caller (status only, no body, no secret)."""


_cache: dict[tuple, tuple[str, float, float]] = {}  # key -> (access token, expires at, lifetime), monotonic clock
_locks: dict[tuple, asyncio.Lock] = {}  # ponytail: asyncio locks bind to one event loop; fine, a sidecar runs one


def _key(p: dict) -> tuple:
    return (p["token_url"], p["client_id"], p.get("scope"), p.get("audience"))


def _secret(p: dict) -> str | None:
    return os.environ.get(p["client_secret_env"]) if p.get("client_secret_env") else p.get("client_secret")


def validate(profiles: dict) -> None:
    """ValueError for an `oauth2` profile without its required fields."""
    for name, p in profiles.items():
        mode = p.get("auth", "none")
        if mode == "oauth2":
            if not (p.get("token_url") and p.get("client_id") and _secret(p)):
                raise ValueError(f"profile {name}: oauth2 needs token_url, client_id and client_secret (or a set client_secret_env)")
            if p.get("token_auth", "post") not in ("post", "basic"):
                raise ValueError(f"profile {name}: token_auth must be post or basic")


async def _fetch(p: dict, c: httpx.AsyncClient) -> tuple[str, float, float]:
    form = {"grant_type": "client_credentials", **{k: p[k] for k in ("scope", "audience") if p.get(k)}}
    auth = None
    if p.get("token_auth", "post") == "basic":
        auth = httpx.BasicAuth(p["client_id"], _secret(p))
    else:
        form |= {"client_id": p["client_id"], "client_secret": _secret(p)}
    try:
        r = await c.post(p["token_url"], data=form, auth=auth)
    except httpx.HTTPError as e:
        raise TokenError(f"oauth2 token: {type(e).__name__}") from None
    if r.status_code != 200:
        raise TokenError(f"oauth2 token: {r.status_code}")
    try:
        d = r.json()
        tok, life = d["access_token"], float(d.get("expires_in") or 60)
    except (ValueError, KeyError, TypeError):
        raise TokenError("oauth2 token: bad answer") from None
    return tok, time.monotonic() + life, life


async def _token(p: dict, c: httpx.AsyncClient) -> str:
    k = _key(p)
    async with _locks.setdefault(k, asyncio.Lock()):  # concurrent requests fetch once
        hit = _cache.get(k)
        if not hit or hit[1] - time.monotonic() < max(30, 0.1 * hit[2]):
            hit = _cache[k] = await _fetch(p, c)
        return hit[0]


async def headers(p: dict, c: httpx.AsyncClient) -> dict:
    """Authorization header(s) for the profile's model endpoint; `c` also carries the token request."""
    mode = p.get("auth", "none")
    if mode == "bearer":
        return {"Authorization": f"Bearer {p['api_key']}"}
    if mode == "google":  # an ID token for the endpoint's audience, from the metadata server or the service account
        import google.auth.transport.requests
        import google.oauth2.id_token
        tok = await asyncio.to_thread(google.oauth2.id_token.fetch_id_token, google.auth.transport.requests.Request(), p["audience"])
        return {"Authorization": f"Bearer {tok}"}
    if mode == "oauth2":
        return {"Authorization": f"Bearer {await _token(p, c)}"}
    return {}


async def post(c: httpx.AsyncClient, p: dict, url: str, **kw) -> httpx.Response:
    """`c.post` with the profile's credentials; for `oauth2`, a 401/403 drops the cached token and retries once."""
    r = await c.post(url, headers=await headers(p, c), **kw)
    if p.get("auth") == "oauth2" and r.status_code in (401, 403):
        _cache.pop(_key(p), None)
        r = await c.post(url, headers=await headers(p, c), **kw)
    return r
