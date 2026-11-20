# Plan: Sidecar usage reporting

> **Status: draft.** The design below is a proposal; "Open questions" need a decision.

## Problem / Goal

Sidecars that call paid model endpoints ([asr](04-asr.md), [caption](05-caption.md), and the existing OCR fallback) spend money on behalf of a job. A consumer wants
the per-attempt usage (model, outcome, tokens or audio seconds) to account for it. Everything a processor writes into `index.json` is untrusted (the parser may
be hostile) and a `meta` object is capped at 2000 bytes, so usage cannot come from the processor. The sidecar is trusted and sees every call; the wrapper is
trusted and writes `status.json`, which the parser cannot touch.

Goal: the sidecar records usage per job; the wrapper collects it when the job ends and writes it into `status.json`.

## Design

### Binding a call to a job without trusting the parser

The parser holds only the profile token, and may send any header, so a job id in a header is forgeable. Instead the wrapper registers each job with each sidecar
it gives the job access to:

- **Sidecar admin routes**, authenticated by a separate `ADMIN_TOKEN` known to the wrapper only (never to the parser):
  - `POST /admin/jobs` `{"job": "<id>", "profile": "<name>"}` returns `{"token": "<job token>"}`; the job token is a fresh opaque secret that selects the same profile as
    the profile token would and is tied to the job.
  - `GET /admin/jobs/<id>/usage` returns `{"calls": [...]}` and drops the record; `DELETE /admin/jobs/<id>` drops it without reading.
- The wrapper puts the **job token** (not the profile token) in the parser's `<FEATURE>_TOKEN`. Every call carries it, so the sidecar can attribute usage to the job
  without a header the parser controls. Job tokens expire after a TTL (default the job's deadline plus a margin) so a crashed wrapper leaks no state.
- Sidecars that do not report usage (no `ADMIN_TOKEN` configured) keep working with the profile token, unchanged.

### Record format

One record per upstream attempt, appended by the sidecar:

```json
{"feature": "asr", "profile": "lan", "model": "gemini-2.5-flash", "outcome": "ok" | "invalid" | "blocked" | "error",
 "unit": "tokens" | "audio_seconds", "input": 1234, "output": 567, "t": 1700000000.0}
```

`status.json` gains, once the job ends (done or failed), `"usage": {"calls": [...], "truncated": false}`. At most `PARSE_USAGE_MAX_CALLS` records (default 20000);
past that the wrapper keeps the first ones and sets `truncated`. The record set never contains prompts, content or credentials.

### Wrapper

At job start, for each requested feature whose sidecar reports usage (`<PREFIX>_ADMIN_TOKEN` set in the wrapper's `<PREFIX>_PROFILES` entry), register the job.
At job end (also on failure, kill or limit), collect and write. If the sidecar is unreachable, `usage` carries `"error": "<string>"` and the job result is
unaffected.

## Open questions

1. Admin routes extend the sidecar protocol ("no other methods or routes", `docs/sidecar-protocol.md`): the protocol gets an optional "usage reporting"
   section. Alternative considered and rejected: a job id header (forgeable).
2. Whether the existing `ocr-sidecar` reports usage too (its remote fallback model is billed). Proposed yes, same code path.

## Implementation order

1. Shared sidecar module (token table with job tokens and TTL, admin routes, usage store) in a common place both sidecars import.
2. Wrapper: registration, collection, `status.json` field, limits.
3. `ocr-sidecar` adopts it. Tests: job token attribution, forged id has no effect, TTL, truncation, sidecar down.
4. Docs.

## Files to change

| File | Change |
|---|---|
| `common/usage.py` (new, shared by sidecars) | job-token table, admin routes, usage store |
| `server/cadmus_parser_server/wrapper.py` | register, collect, write `usage` |
| `ocr-sidecar/app.py` | adopt |
| tests in `server/tests`, `ocr-sidecar/tests` | above |

## Docs to update

`docs/sidecar-protocol.md` (optional usage-reporting section), `docs/reference.md` (`status.json` `usage`, env vars), `docs/security.md` (why usage is trusted), `docs/client.md` (reading `usage`).
