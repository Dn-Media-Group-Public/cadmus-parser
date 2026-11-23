"""The caption sidecar: one image, or up to 20 keyframes, in; the raw text of ONE upstream vision call out (docs/sidecar-protocol.md). No retry, validation or
fallback: those are processors (server/cadmus_parser_server/caption.py). POST /process with `Authorization: Bearer <token>`:
- an image: the body is the image (`Content-Type: image/*`);
- a video window: `Content-Type: multipart/mixed`, one `image/*` part per keyframe in order, and `X-Frame-Times: 0.0,3.4,...` (seconds, one per part).
Answer: JSON `{"text", "finish_reason", "block_reason", "model"}`. Errors are JSON `{"error": ...}` (400, 401, 413, 415, 502, 503).
CAPTION_PROFILES: {name: {token, base_url, model, auth: none|bearer|google, api_key?, audience?, prompt?, max_output_tokens?}} (JSON; chat completions with
`image_url` data URIs). CAPTION_ADMIN_TOKEN enables usage reporting."""

import asyncio
import base64
import email
import email.policy
import hmac
import json
import os
import sys

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))  # the image has usage.py beside this file
from usage import Usage  # noqa: E402

MAX_FRAMES = 20
PROMPT = ("Describe what the image shows for someone who cannot see it: the scene, people, objects, any visible text and what is happening. "
          "Be factual and concise. Markdown emphasis is allowed; raw HTML is not.")
VIDEO_PROMPT = ("The images are keyframes of one video, in order, each preceded by its time. Describe what happens in this part of the video for someone who cannot see it: "
                "scenes, people, objects, visible text and changes between the frames. Be factual and concise. Markdown emphasis is allowed; raw HTML is not.")


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


async def upstream_headers(p: dict) -> dict:
    mode = p.get("auth", "none")
    if mode == "bearer":
        return {"Authorization": f"Bearer {p['api_key']}"}
    if mode == "google":  # an ID token for the endpoint's audience
        import google.auth.transport.requests
        import google.oauth2.id_token
        tok = await asyncio.to_thread(google.oauth2.id_token.fetch_id_token, google.auth.transport.requests.Request(), p["audience"])
        return {"Authorization": f"Bearer {tok}"}
    return {}


def split_frames(ctype_header: str, body: bytes) -> list:
    """[(content type, bytes)] of the image parts of a multipart body; ValueError if it is not one."""
    msg = email.message_from_bytes(b"Content-Type: " + ctype_header.encode() + b"\r\n\r\n" + body, policy=email.policy.HTTP)
    if not msg.is_multipart():
        raise ValueError("not multipart")
    return [(part.get_content_type(), part.get_payload(decode=True) or b"") for part in msg.iter_parts()]


async def call_upstream(c: httpx.AsyncClient, p: dict, frames: list, times: list | None) -> tuple[dict, dict]:
    """frames: [(content type, bytes)]; times: seconds per frame, None for a single image. (answer for the processor, upstream usage dict)."""
    content = [{"type": "text", "text": p.get("prompt") or (VIDEO_PROMPT if times else PROMPT)}]
    for i, (ct, data) in enumerate(frames):
        if times:
            content.append({"type": "text", "text": f"[{mmss(times[i])}]"})
        content.append({"type": "image_url", "image_url": {"url": f"data:{ct};base64,{base64.b64encode(data).decode()}"}})
    r = await c.post(p["base_url"].rstrip("/") + "/chat/completions", headers=await upstream_headers(p), json={
        "model": p["model"], "messages": [{"role": "user", "content": content}], "temperature": 0, "max_tokens": int(p.get("max_output_tokens") or 2048)})
    r.raise_for_status()
    d = r.json()
    ch = (d.get("choices") or [{}])[0]
    block = (d.get("prompt_feedback") or {}).get("block_reason") or None
    return {"text": (ch.get("message") or {}).get("content") or "", "finish_reason": ch.get("finish_reason"), "block_reason": block, "model": p["model"]}, d.get("usage") or {}


def make_app(profiles: dict, max_body: int = 32 << 20, max_concurrent: int = 4, timeout: float = 120, transport=None, admin_token: str | None = None,
             max_calls: int = 20000) -> Starlette:
    usage = Usage(admin_token, max_calls)
    busy = asyncio.Semaphore(max_concurrent)

    async def process(request: Request):
        found = _profile_for(profiles, request.headers.get("authorization", ""), usage)
        if found is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        name, p, job = found
        ctype_header = request.headers.get("content-type", "")
        ctype = ctype_header.split(";")[0].strip().lower()
        if not (ctype.startswith("image/") or ctype == "multipart/mixed"):
            return JSONResponse({"error": "image or multipart/mixed only"}, 415)
        if int(request.headers.get("content-length") or 0) > max_body:
            return JSONResponse({"error": "body too large"}, 413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_body:
                return JSONResponse({"error": "body too large"}, 413)
        times = None
        try:
            if ctype == "multipart/mixed":
                frames = split_frames(ctype_header, body)
                times = [float(x) for x in request.headers.get("x-frame-times", "").split(",") if x.strip()]
                if not frames or len(frames) > MAX_FRAMES or len(times) != len(frames) or not all(ct.startswith("image/") for ct, _ in frames):
                    raise ValueError("bad frames")
            else:
                frames = [(ctype, body)]
        except ValueError:
            return JSONResponse({"error": "bad request"}, 400)
        if busy.locked():
            return JSONResponse({"error": "busy"}, 503)
        async with busy:
            call = {"feature": "caption", "profile": name, "model": p["model"]}
            try:
                async with httpx.AsyncClient(timeout=timeout, transport=transport) as c:
                    answer, u = await call_upstream(c, p, frames, times)
            except Exception as e:  # noqa: BLE001  the processor counts the attempt as failed
                usage.record(job, **call, outcome="error", unit="tokens", input=0, output=0)
                return JSONResponse({"error": f"upstream failed ({type(e).__name__})"}, 502)
            outcome = "blocked" if answer["block_reason"] or str(answer["finish_reason"]).lower() in ("content_filter", "safety") else "ok"
            usage.record(job, **call, outcome=outcome, unit="tokens", input=u.get("prompt_tokens", 0), output=u.get("completion_tokens", 0))
            return JSONResponse(answer)

    return Starlette(routes=[Route("/process", process, methods=["POST"]), Route("/health", lambda r: JSONResponse({"ok": True})), *usage.routes(profiles)])


def app_from_env() -> Starlette:
    e = os.environ
    return make_app(json.loads(e["CAPTION_PROFILES"]), int(e.get("CAPTION_MAX_BODY") or 32 << 20), int(e.get("CAPTION_MAX_CONCURRENT") or 4), float(e.get("CAPTION_TIMEOUT") or 120),
                    admin_token=e.get("CAPTION_ADMIN_TOKEN") or None, max_calls=int(e.get("PARSE_USAGE_MAX_CALLS") or 20000))
