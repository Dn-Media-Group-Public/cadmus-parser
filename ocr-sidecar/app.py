"""The OCR sidecar: a stateless forwarder speaking the common sidecar protocol (docs/sidecar-protocol.md). POST /process with the image as the
body (PNG/JPEG, already downscaled by the parser), `Content-Type` its type and `Authorization: Bearer <profile token>`; the image goes to that
profile's OpenAI-compatible endpoint and the transcript comes back as the `text/plain` body. It forwards no request header, takes no URL and no
path, has no blob access and never decodes an image. The token picks the profile, so a parser can only reach the profile its job was given.
Errors are JSON `{"error": "..."}` with standard status codes (401, 413, 415, 502, 503). With OCR_ADMIN_TOKEN set it also reports per-job usage (common/usage.py). OCR_PROFILES: {name: {token, base_url, model,
auth: none|bearer|google|oauth2, api_key?, audience?, token_url?, client_id?, client_secret?, client_secret_env?, scope?, token_auth?, prompt?}} (JSON)."""

import asyncio
import base64
import hmac
import json
import os
import sys

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))  # the image has usage.py and upstream_auth.py beside this file
import upstream_auth  # noqa: E402
from usage import Usage  # noqa: E402

IMAGE_TYPES = ("image/png", "image/jpeg")
PROMPT = "Transcribe all text in this image exactly as written. Output only the text."


def profile_for(profiles: dict, auth_header: str, usage: Usage | None = None) -> tuple[dict, str | None] | None:
    """(profile, job id or None) for a profile token or a live job token."""
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    found = None
    for p in profiles.values():  # no early exit: the time does not tell which profile matched
        if hmac.compare_digest(token.encode(), p["token"].encode()):
            found = (p, None)
    if found is None and usage and (r := usage.resolve(token)):
        found = (profiles[r[0]], r[1])
    return found


def make_app(profiles: dict, max_body: int = 10 << 20, max_concurrent: int = 4, timeout: float = 120, transport=None, admin_token: str | None = None,
             max_calls: int = 20000) -> Starlette:
    upstream_auth.validate(profiles)
    usage = Usage(admin_token, max_calls)
    busy = asyncio.Semaphore(max_concurrent)

    async def process(request: Request):
        found = profile_for(profiles, request.headers.get("authorization", ""), usage)
        if found is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        p, job = found
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in IMAGE_TYPES:
            return JSONResponse({"error": "PNG or JPEG only"}, 415)
        if int(request.headers.get("content-length") or 0) > max_body:
            return JSONResponse({"error": "body too large"}, 413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_body:
                return JSONResponse({"error": "body too large"}, 413)
        if busy.locked():
            return JSONResponse({"error": "busy"}, 503)
        async with busy:
            msg = {"role": "user", "content": [{"type": "text", "text": p.get("prompt") or PROMPT},
                                               {"type": "image_url", "image_url": {"url": f"data:{ctype};base64,{base64.b64encode(body).decode()}"}}]}
            call = {"feature": "ocr", "profile": next(n for n, v in profiles.items() if v is p), "model": p["model"], "unit": "tokens"}
            try:
                async with httpx.AsyncClient(timeout=timeout, transport=transport) as c:
                    r = await upstream_auth.post(c, p, p["base_url"].rstrip("/") + "/chat/completions",
                                                  json={"model": p["model"], "messages": [msg], "temperature": 0})
                    r.raise_for_status()
                    data = r.json()
                    text = data["choices"][0]["message"]["content"] or ""
                    u = data.get("usage") or {}
                    usage.record(job, **call, outcome="ok", input=u.get("prompt_tokens", 0), output=u.get("completion_tokens", 0))
                    return PlainTextResponse(text)
            except upstream_auth.TokenError as e:  # never reached the model: no usage row
                return JSONResponse({"error": str(e)}, 502)
            except Exception as e:  # noqa: BLE001  the parser keeps its Tesseract text and notes why
                usage.record(job, **call, outcome="error", input=0, output=0)
                return JSONResponse({"error": f"upstream failed ({type(e).__name__})"}, 502)

    return Starlette(routes=[Route("/process", process, methods=["POST"]), Route("/health", lambda r: JSONResponse({"ok": True})), *usage.routes(profiles)])


def app_from_env() -> Starlette:
    e = os.environ
    return make_app(json.loads(e["OCR_PROFILES"]), int(e.get("OCR_MAX_BODY") or 10 << 20), int(e.get("OCR_MAX_CONCURRENT") or 4),
                    float(e.get("OCR_TIMEOUT") or 120),
                    admin_token=e.get("OCR_ADMIN_TOKEN") or None, max_calls=int(e.get("PARSE_USAGE_MAX_CALLS") or 20000))
