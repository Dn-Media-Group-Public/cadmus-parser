# Plan: OAuth2 client-credentials auth for the model sidecars

> **Status: implemented.** The open questions were resolved with the suggested defaults: `client_secret_env` supported (wins over `client_secret`); `token_auth: post|basic`; client credentials only; TLS unchanged.
> Deviation: the token request reuses the model call's `httpx` client; `upstream_auth.post(c, profile, url, **kw)` does header + one 401/403 retry. `egress_rules.endpoints` also pins `token_url`.
> Independent of the other plans; the first consumer is article-graph (`docs/plans/remove-llm-token-proxy.md` there).

## Problem / Goal

The `ocr`, `asr` and `caption` sidecars call a model endpoint with the profile's `auth`: `none`, `bearer` (a static `api_key`) or `google` (an ID token for an `audience`).
Many gateways in front of models use the OAuth2 **client-credentials** grant (a Keycloak/OIDC client id and secret exchanged for a short-lived access token, typically 5 minutes).
Neither `bearer` (the key expires) nor `google` fits. A consumer today has to put a token-refreshing reverse proxy in front of the gateway and point the profiles at it with `auth: none`:
an extra service that every deployment must build, run and secure.

Goal: a fourth mode, `auth: oauth2`, in all three sidecars, with the token fetched, cached and refreshed by the sidecar.

## Design

- **Profile fields** (in `OCR_PROFILES` / `ASR_PROFILES` / `CAPTION_PROFILES` and the fallback tables): `auth: "oauth2"`, `token_url` (required), `client_id` (required),
  `client_secret` (required), `scope?`, `audience?` (sent as the `audience` form field, for IdPs that need it; note `google` also uses `audience`, with another meaning, hence it is optional and only read here when set).
- **Grant:** `POST token_url` with `application/x-www-form-urlencoded` `grant_type=client_credentials`, client credentials in the body (`client_id`, `client_secret`; HTTP Basic is the alternative, see open question 2).
  The answer's `access_token` and `expires_in` are read; the model call carries `Authorization: Bearer <access_token>`.
- **Caching:** one token per distinct `(token_url, client_id, scope, audience)`, held in process memory only, never logged. Refreshed when fewer than `max(30 s, 10 % of lifetime)` remain
  (`expires_in` missing: treat as 60 s). An `asyncio.Lock` per key so concurrent requests fetch once.
- **401/403 from the model endpoint:** invalidate the cached token and retry the call once (the IdP may have rotated keys or revoked the token). A second failure is reported like any upstream error.
- **Token endpoint failure:** the request fails with a 502 and the message `oauth2 token: <status>` (no body, no secret) so the processor's normal retry/fallback applies; usage rows are not written for a call that never reached the model.
- **Shared code:** the header logic is copied three times today (`ocr-sidecar/app.py`, `asr-sidecar/app.py`, `caption-sidecar/app.py`, each with its own `none|bearer|google` branch). The new mode goes in one
  module under `common/` (next to `usage.py`, already shared with the sidecars): `upstream_auth.headers(profile) -> dict` covering all four modes, and the three sidecars call it. The sidecar Dockerfiles already copy `common/`; no new dependency (`httpx` is installed).
- **Egress:** the sidecar must reach `token_url`. Check how the sidecars' network egress is limited in the compose overlay (`egress_rules`) and document that the token host is allowed alongside the model host.
- **Profile validation at start-up:** a missing `token_url`/`client_id`/`client_secret` for `oauth2` fails the sidecar at start (like a bad JSON table does), not on the first request.

### Open questions

1. Secret placement: `client_secret` inline in the profile JSON (the same as `api_key` today), or also `client_secret_env: "NAME"` (read from that environment variable, so a swarm secret / env file can hold it and the JSON table can be logged)? Suggest: support both; `client_secret_env` wins.
2. Client authentication method: body credentials only (`client_secret_post`), or also `client_secret_basic` via `token_auth: "basic"`? Suggest: `post` default, `basic` selectable (some IdPs accept only one).
3. Should the module also support the `refresh_token` / password grants? Suggest: no, client credentials only.
4. TLS: the token and model hosts may use a private CA; reuse whatever `httpx` trust the sidecar already has (no new option).

## Implementation order

1. Decide the open questions.
2. `common/upstream_auth.py` with the four modes, the cache and the lock; unit tests with a fake token endpoint (expiry and refresh, concurrent requests = one fetch, 401 retry once, token failure = 502 without secrets in the message, key isolation between profiles).
3. Replace the three inline `none|bearer|google` branches with the shared call; keep their behaviour unchanged (existing sidecar tests must pass untouched).
4. Start-up validation of `oauth2` profiles.
5. Docs; move the plan to `docs/implemented-plans/`.

## Files to change

| File | Change |
|---|---|
| `common/upstream_auth.py` (new) + test | the four auth modes, token cache, refresh, lock |
| `ocr-sidecar/app.py`, `asr-sidecar/app.py`, `caption-sidecar/app.py` | use `upstream_auth.headers`; retry once on 401/403; docstring lists `oauth2` |
| `docs/reference.md` | the profile tables of the three sidecars (`auth: none\|bearer\|google\|oauth2` and the new fields), egress note |
| `docs/sidecar-protocol.md` | a sentence on upstream auth modes |
| `docs/versioning.md` | a minor version bump (new optional profile mode, nothing existing changes) |

## Docs to update

`docs/reference.md`, `docs/sidecar-protocol.md`, `docs/versioning.md`, `docs/plans/index.md`.
