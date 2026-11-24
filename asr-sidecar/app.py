"""The ASR sidecar: one audio chunk in, the raw answer of ONE upstream call out (docs/sidecar-protocol.md). No retry, no validation, no fallback: those are
processors (server/cadmus_parser_server/asr.py). POST /process with the audio as the body (`Content-Type: audio/*`), `Authorization: Bearer <token>`, options
as `X-Chunk-Seconds`, `X-Language`, `X-Context` (percent-encoded UTF-8). Answer: JSON `{"segments": [...] | null, "raw": "..." | null, "finish_reason", "block_reason",
"model", "language"}`: `segments` from the `transcriptions` backend, `raw` (the model's text, a JSON `{"segments": [...]}`) from `chat`.
ASR_PROFILES: {name: {token, base_url, model, backend: transcriptions|chat, auth: none|bearer|google|oauth2, api_key?, audience?, token_url?, client_id?, client_secret?, client_secret_env?, scope?, token_auth?, prompt?, max_output_tokens?,
audio_format?, response_format?, form?}} (JSON). Errors are JSON `{"error": ...}` (401, 413, 415, 502, 503). ASR_ADMIN_TOKEN enables usage reporting."""

import asyncio
import base64
import hmac
import json
import os
import sys
import urllib.parse

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))  # the image has usage.py and upstream_auth.py beside this file
import upstream_auth  # noqa: E402
from usage import Usage  # noqa: E402

PROMPT = ("Transcribe the audio verbatim, in the language spoken. Label each speaker consistently as \"Person 1\", \"Person 2\", ... for the whole recording. "
          "Answer only with JSON of the form {\"segments\": [{\"start\": \"MM:SS\", \"speaker\": \"Person 1\", \"text\": \"...\"}]}. `start` is when the segment begins, "
          "in order. Split any speaker turn longer than about 45 seconds into several segments.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["segments"], "properties": {"segments": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["start", "speaker", "text"],
    "properties": {"start": {"type": "string"}, "speaker": {"type": "string"}, "text": {"type": "string"}}}}}}


def mmss(s: float) -> str:
    return f"{int(s) // 60:02d}:{int(s) % 60:02d}"


def _profile_for(profiles: dict, auth_header: str, usage: Usage | None):
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    found = None
    for name, p in profiles.items():  # no early exit: the time does not tell which profile matched
        if hmac.compare_digest(token.encode(), p["token"].encode()):
            found = (name, p, None)
    if found is None and usage and (r := usage.resolve(token)):
        found = (r[0], profiles[r[0]], r[1])
    return found


def chat_prompt(p: dict, seconds: float, language: str, context: str) -> str:
    text = p.get("prompt") or PROMPT
    if seconds > 0:
        text += f" The recording is {mmss(seconds)} long: no `start` may be later than {mmss(seconds)}."
    if language:
        text += f" The language is {language}."
    if context:
        text += f" Context (names, terminology, topics): {context}"
    return text


async def call_upstream(c: httpx.AsyncClient, p: dict, audio: bytes, ctype: str, seconds: float, language: str, context: str) -> tuple[dict, dict]:
    """(answer for the processor, upstream usage dict)."""
    base = p["base_url"].rstrip("/")
    if p.get("backend", "transcriptions") == "chat":
        fmt = p.get("audio_format") or "ogg"
        content = [{"type": "text", "text": chat_prompt(p, seconds, language, context)},
                   {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}]
        r = await upstream_auth.post(c, p, base + "/chat/completions", json={
            "model": p["model"], "messages": [{"role": "user", "content": content}], "temperature": 0, "max_tokens": int(p.get("max_output_tokens") or 32768),
            "response_format": {"type": "json_schema", "json_schema": {"name": "transcript", "strict": True, "schema": SCHEMA}}})
        r.raise_for_status()
        d = r.json()
        ch = (d.get("choices") or [{}])[0]
        block = ((d.get("prompt_feedback") or {}).get("block_reason")) or None
        return ({"segments": None, "raw": (ch.get("message") or {}).get("content") or "", "finish_reason": ch.get("finish_reason"), "block_reason": block,
                 "model": p["model"], "language": None}, d.get("usage") or {})
    form = {"model": p["model"], "response_format": p.get("response_format") or "verbose_json", "temperature": "0", **{k: str(v) for k, v in (p.get("form") or {}).items()}}
    if language:
        form["language"] = language
    if context:
        form["prompt"] = context
    r = await upstream_auth.post(c, p, base + "/audio/transcriptions", data=form, files={"file": ("chunk.opus", audio, ctype)})
    r.raise_for_status()
    d = r.json()
    segs = [{"start": s.get("start", 0), "speaker": s.get("speaker"), "text": s.get("text", "")} for s in d.get("segments") or [] if isinstance(s, dict)]
    if not segs and d.get("text"):
        segs = [{"start": 0, "speaker": None, "text": d["text"]}]
    return {"segments": segs, "raw": None, "finish_reason": None, "block_reason": None, "model": p["model"], "language": d.get("language")}, d.get("usage") or {}


def make_app(profiles: dict, max_body: int = 25 << 20, max_concurrent: int = 4, timeout: float = 600, transport=None, admin_token: str | None = None,
             max_calls: int = 20000) -> Starlette:
    upstream_auth.validate(profiles)
    usage = Usage(admin_token, max_calls)
    busy = asyncio.Semaphore(max_concurrent)

    async def process(request: Request):
        found = _profile_for(profiles, request.headers.get("authorization", ""), usage)
        if found is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        name, p, job = found
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if not ctype.startswith("audio/"):
            return JSONResponse({"error": "audio only"}, 415)
        if int(request.headers.get("content-length") or 0) > max_body:
            return JSONResponse({"error": "body too large"}, 413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_body:
                return JSONResponse({"error": "body too large"}, 413)
        try:
            seconds = float(request.headers.get("x-chunk-seconds") or 0)
        except ValueError:
            seconds = 0.0
        language = urllib.parse.unquote(request.headers.get("x-language") or "")[:64]
        context = urllib.parse.unquote(request.headers.get("x-context") or "")[:4000]
        if busy.locked():
            return JSONResponse({"error": "busy"}, 503)
        async with busy:
            call = {"feature": "asr", "profile": name, "model": p["model"]}
            try:
                async with httpx.AsyncClient(timeout=timeout, transport=transport) as c:
                    answer, u = await call_upstream(c, p, body, ctype, seconds, language, context)
            except upstream_auth.TokenError as e:  # never reached the model: no usage row
                return JSONResponse({"error": str(e)}, 502)
            except Exception as e:  # noqa: BLE001  the processor counts the attempt as failed
                usage.record(job, **call, outcome="error", unit="audio_seconds", input=0, output=0)
                return JSONResponse({"error": f"upstream failed ({type(e).__name__})"}, 502)
            chat = p.get("backend", "transcriptions") == "chat"
            outcome = "blocked" if answer["block_reason"] or str(answer["finish_reason"]).lower() in ("content_filter", "safety") else "ok"
            if chat:
                usage.record(job, **call, outcome=outcome, unit="tokens", input=u.get("prompt_tokens", 0), output=u.get("completion_tokens", 0))
            else:
                usage.record(job, **call, outcome=outcome, unit="audio_seconds", input=u.get("seconds") or seconds, output=0)
            return JSONResponse(answer)

    return Starlette(routes=[Route("/process", process, methods=["POST"]), Route("/health", lambda r: JSONResponse({"ok": True})), *usage.routes(profiles)])


def app_from_env() -> Starlette:
    e = os.environ
    return make_app(json.loads(e["ASR_PROFILES"]), int(e.get("ASR_MAX_BODY") or 25 << 20), int(e.get("ASR_MAX_CONCURRENT") or 4), float(e.get("ASR_TIMEOUT") or 600),
                    admin_token=e.get("ASR_ADMIN_TOKEN") or None, max_calls=int(e.get("PARSE_USAGE_MAX_CALLS") or 20000))
