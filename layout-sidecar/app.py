"""The layout sidecar: one page image in, the regions on it out (docs/sidecar-protocol.md). POST /process with the page as the body (PNG/JPEG, already downscaled by the
parser), `Content-Type` its type, `Authorization: Bearer <token>` (the token picks the profile) and optionally `X-Language: nb,en` (BCP 47). No retry, no escalation, no cropping:
those are the parser's (server/cadmus_parser_server/layout.py).
Answer, identical for every engine: `{"engine", "model", "width", "height", "boxes": [{"label": picture|table|text|title|other, "bbox": [x0, y0, x1, y1], "score"}], "score"?,
"escalate_below"?, "max_figure_members"}`; bbox in pixels of the image that was sent. A `mode: full` profile adds `markdown` to the boxes (reading order) and `"full": true`.
Unparseable upstream output is a 502, never an empty list. Errors are JSON `{"error": ...}` (400, 401, 413, 415, 502, 503).
LAYOUT_PROFILES: {name: {token, engine, escalate_below?, max_figure_members?, ...}} (JSON), with engine
- `yolo-onnx` (in this process, CPU): model_path, score_min?, input_size?, classes?, label_map?
- `docling-serve`: base_url, auth, mode: layout|full, lang_map?, plus the upstream_auth fields
- `openai-vlm`: base_url, model, auth, prompt?, format: json|doctags, plus the upstream_auth fields."""

import asyncio
import base64
import hmac
import io
import json
import os
import re
import sys

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))  # the image has usage.py and upstream_auth.py beside this file
import upstream_auth  # noqa: E402
from usage import Usage  # noqa: E402

IMAGE_TYPES = ("image/png", "image/jpeg")
ESCALATE_BELOW = {"yolo-onnx": 0.5, "docling-serve": 0.5, "openai-vlm": None}  # a VLM gives no confidence
MAX_FIGURE_MEMBERS = {"yolo-onnx": 2000, "docling-serve": 1000, "openai-vlm": 200}
LABELS = ("picture", "table", "text", "title", "other")
# DocLayout-YOLO (DocStructBench) class names -> our labels; a profile's `classes` / `label_map` replace them
YOLO_CLASSES = ["title", "plain text", "abandon", "figure", "figure_caption", "table", "table_caption", "table_footnote", "isolate_formula", "formula_caption"]
YOLO_LABELS = {"title": "title", "plain text": "text", "figure": "picture", "table": "table", "figure_caption": "text", "table_caption": "text", "table_footnote": "text"}
VLM_PROMPT = ('List the pictures, photographs, illustrations, charts and tables on this page. Answer only with JSON: {"boxes": [{"label": "picture" or "table", '
              '"bbox": [x0, y0, x1, y1]}]} with pixel coordinates of this image, origin top left. An empty list if there are none.')


class BadUpstream(Exception):
    """The engine's answer could not be understood: a 502 (the parser escalates), not an empty page."""


def image_size(body: bytes) -> tuple[int, int]:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(body)) as im:
            return im.size
    except Exception as e:  # noqa: BLE001
        raise ValueError("not an image") from e


# ---- yolo-onnx ----------------------------------------------------------------------------------------------------

_sessions: dict = {}


def load_session(path: str):  # replaced in the tests
    import onnxruntime
    if path not in _sessions:
        _sessions[path] = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"])
    return _sessions[path]


def _iou(a, b) -> float:
    w, h = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    inter = max(0.0, w) * max(0.0, h)
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def yolo_boxes(out, scale: float, pad: tuple, size: tuple, p: dict) -> list:
    """The raw model output -> [{label, bbox, score}] in pixels of the sent image. Two layouts: end-to-end (1, N, 6) = x0 y0 x1 y1 score class, or (1, 4+nc, N) = cx cy w h scores...
    (then a class-wise NMS here)."""
    import numpy as np
    out = np.asarray(out)[0]
    cand = []
    if out.ndim == 2 and out.shape[1] == 6:
        cand = [(r[:4].tolist(), float(r[4]), int(r[5])) for r in out]
    else:
        out = out.T if out.shape[0] < out.shape[1] else out
        for r in out:
            c = int(np.argmax(r[4:]))
            cand.append(([r[0] - r[2] / 2, r[1] - r[3] / 2, r[0] + r[2] / 2, r[1] + r[3] / 2], float(r[4 + c]), c))
    smin = float(p.get("score_min", 0.2))
    cand = sorted((c for c in cand if c[1] >= smin), key=lambda c: -c[1])
    keep: list = []
    for b, s, c in cand:
        if all(k[2] != c or _iou(b, k[0]) < 0.5 for k in keep):
            keep.append((b, s, c))
    classes, lmap = p.get("classes") or YOLO_CLASSES, p.get("label_map") or YOLO_LABELS
    res = []
    for b, s, c in keep:
        x0, y0, x1, y1 = ((b[0] - pad[0]) / scale, (b[1] - pad[1]) / scale, (b[2] - pad[0]) / scale, (b[3] - pad[1]) / scale)
        x0, x1, y0, y1 = max(0, min(size[0], x0)), max(0, min(size[0], x1)), max(0, min(size[1], y0)), max(0, min(size[1], y1))
        name = classes[c] if c < len(classes) else str(c)
        res.append({"label": lmap.get(name, "other"), "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)], "score": round(s, 4)})
    return res


def run_yolo(p: dict, body: bytes) -> dict:
    import numpy as np
    from PIL import Image
    sess = load_session(p["model_path"])
    n = int(p.get("input_size") or 640)
    with Image.open(io.BytesIO(body)) as im:
        im = im.convert("RGB")
        size = im.size
        scale = min(n / size[0], n / size[1])
        w, h = max(1, round(size[0] * scale)), max(1, round(size[1] * scale))
        canvas = Image.new("RGB", (n, n), (114, 114, 114))
        pad = ((n - w) // 2, (n - h) // 2)
        canvas.paste(im.resize((w, h)), pad)
    x = (np.asarray(canvas, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
    return {"engine": "yolo-onnx", "model": os.path.basename(p["model_path"]), "width": size[0], "height": size[1], "boxes": yolo_boxes(out, scale, pad, size, p)}


# ---- docling-serve ------------------------------------------------------------------------------------------------

def _ref(doc: dict, ref: str) -> dict:
    kind, i = ref.lstrip("#/").split("/")
    return doc[kind][int(i)]


def _walk(doc: dict, node: dict):
    """Items under `node` in reading order (groups are descended into)."""
    for ch in node.get("children") or []:
        it = _ref(doc, ch["$ref"])
        if ch["$ref"].startswith("#/groups/"):
            yield from _walk(doc, it)
        else:
            yield ch["$ref"].split("/")[1], it


def table_markdown(t: dict) -> str:
    d = t.get("data") or {}
    rows, cols = int(d.get("num_rows") or 0), int(d.get("num_cols") or 0)
    if not rows or not cols:
        return ""
    grid = [[""] * cols for _ in range(rows)]
    for c in d.get("table_cells") or []:
        r0, c0 = c.get("start_row_offset_idx", 0), c.get("start_col_offset_idx", 0)
        if r0 < rows and c0 < cols:
            grid[r0][c0] = (c.get("text") or "").replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(r) + " |" for r in grid]
    return "\n".join([lines[0], "|" + "---|" * cols, *lines[1:]])


def text_markdown(t: dict) -> str:
    s, label = (t.get("text") or "").strip(), t.get("label")
    return {"title": f"# {s}", "section_header": f"## {s}", "list_item": f"- {s}", "code": f"```\n{s}\n```"}.get(label, s)


def convert_docling(d: dict, width: int, height: int, full: bool) -> dict:
    """The docling-serve answer -> the common response. PDF points with a BOTTOMLEFT origin become pixels of the image sent."""
    try:
        doc = d["document"]["json_content"]
        pages = doc["pages"]
        conf = d.get("confidence") or doc.get("confidence") or {}
        score = conf.get("mean_score")
        if score is None and isinstance(conf.get("pages"), dict) and conf["pages"]:
            vals = [(v.get("mean_score") if isinstance(v, dict) else None) for v in conf["pages"].values()]
            vals = [v for v in vals if v is not None]
            score = sum(vals) / len(vals) if vals else None
        boxes = []
        for kind, it in _walk(doc, doc["body"]):
            prov = (it.get("prov") or [None])[0]
            if not prov:
                continue
            pg = pages[str(prov["page_no"])]["size"]
            sx, sy, b = width / pg["width"], height / pg["height"], prov["bbox"]
            if b.get("coord_origin", "BOTTOMLEFT") == "BOTTOMLEFT":
                y0, y1 = (pg["height"] - b["t"]) * sy, (pg["height"] - b["b"]) * sy
            else:
                y0, y1 = b["t"] * sy, b["b"] * sy
            label = {"pictures": "picture", "tables": "table"}.get(kind) or {"title": "title", "section_header": "title"}.get(it.get("label"), "text")
            box = {"label": label, "bbox": [round(min(b["l"], b["r"]) * sx, 1), round(min(y0, y1), 1), round(max(b["l"], b["r"]) * sx, 1), round(max(y0, y1), 1)],
                   "score": 1.0 if score is None else float(score)}
            if full and label != "picture":
                box["markdown"] = table_markdown(it) if label == "table" else text_markdown(it)
            boxes.append(box)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        raise BadUpstream(f"docling answer: {type(e).__name__}") from None
    return {"engine": "docling-serve", "model": "docling", "width": width, "height": height, "boxes": boxes, **({"score": float(score)} if score is not None else {}),
            **({"full": True} if full else {})}


async def run_docling(c: httpx.AsyncClient, p: dict, body: bytes, ctype: str, size: tuple, langs: list) -> tuple[dict, dict]:
    full = p.get("mode", "layout") == "full"
    form = {"do_ocr": str(full).lower(), "do_table_structure": str(full).lower(), "to_formats": "json"}
    if full and langs:
        form["ocr_lang"] = [(p.get("lang_map") or {}).get(x, x) for x in langs]
    r = await upstream_auth.post(c, p, p["base_url"].rstrip("/") + "/v1/convert/file", data=form,
                                 files={"files": ("page" + (".png" if ctype == "image/png" else ".jpg"), body, ctype)})
    r.raise_for_status()
    try:
        return convert_docling(r.json(), size[0], size[1], full), {}
    except ValueError:
        raise BadUpstream("docling answer is not JSON") from None


# ---- openai-vlm ---------------------------------------------------------------------------------------------------

def parse_vlm(text: str, fmt: str, size: tuple) -> list:
    boxes = []
    if fmt == "doctags":  # <picture><loc_12><loc_30><loc_480><loc_300>...</picture>, coordinates 0..500
        for m in re.finditer(r"<(\w+)>((?:<loc_\d+>){4})", text):
            x0, y0, x1, y1 = (int(v) for v in re.findall(r"\d+", m.group(2)))
            label = {"picture": "picture", "table": "table", "otsl": "table", "title": "title", "section_header": "title", "text": "text"}.get(m.group(1), "other")
            boxes.append({"label": label, "bbox": [x0 / 500 * size[0], y0 / 500 * size[1], x1 / 500 * size[0], y1 / 500 * size[1]], "score": 1.0})
        if not boxes and "<doctag>" not in text:
            raise BadUpstream("no doctags in the answer")
        return boxes
    m = re.search(r"\{.*\}", text, re.S)  # the model may wrap the JSON in prose or a fence
    try:
        items = json.loads(m.group(0))["boxes"] if m else None
        for it in items:
            x0, y0, x1, y1 = (float(v) for v in it["bbox"])
            boxes.append({"label": it.get("label") if it.get("label") in LABELS else "other", "bbox": [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], "score": 1.0})
    except (ValueError, KeyError, TypeError) as e:
        raise BadUpstream("unparseable VLM answer") from e
    return boxes


async def run_vlm(c: httpx.AsyncClient, p: dict, body: bytes, ctype: str, size: tuple, langs: list) -> tuple[dict, dict]:
    prompt = p.get("prompt") or VLM_PROMPT
    if langs:
        prompt += f" The text on the page is in: {', '.join(langs)}."
    msg = {"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:{ctype};base64,{base64.b64encode(body).decode()}"}}]}
    r = await upstream_auth.post(c, p, p["base_url"].rstrip("/") + "/chat/completions", json={"model": p["model"], "messages": [msg], "temperature": 0})
    r.raise_for_status()
    try:
        d = r.json()
        text = d["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise BadUpstream("not a chat completion") from None
    boxes = parse_vlm(text, p.get("format", "json"), size)
    return {"engine": "openai-vlm", "model": p["model"], "width": size[0], "height": size[1], "boxes": boxes}, d.get("usage") or {}


# ---- the app ------------------------------------------------------------------------------------------------------

def _profile_for(profiles: dict, auth_header: str, usage: Usage | None):
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    found = None
    for name, p in profiles.items():  # no early exit: the time does not tell which profile matched
        if hmac.compare_digest(token.encode(), p["token"].encode()):
            found = (name, p, None)
    if found is None and usage and (r := usage.resolve(token)):
        found = (r[0], profiles[r[0]], r[1])
    return found


def validate(profiles: dict) -> None:
    upstream_auth.validate(profiles)
    for name, p in profiles.items():
        if p.get("engine") not in ESCALATE_BELOW:
            raise ValueError(f"profile {name}: engine must be one of {', '.join(ESCALATE_BELOW)}")
        need = {"yolo-onnx": ("model_path",), "docling-serve": ("base_url",), "openai-vlm": ("base_url", "model")}[p["engine"]]
        if missing := [k for k in need if not p.get(k)]:
            raise ValueError(f"profile {name}: {p['engine']} needs {', '.join(missing)}")


def make_app(profiles: dict, max_body: int = 10 << 20, max_concurrent: int = 2, timeout: float = 120, transport=None, admin_token: str | None = None,
             max_calls: int = 20000) -> Starlette:
    validate(profiles)
    usage = Usage(admin_token, max_calls)
    busy = asyncio.Semaphore(max_concurrent)

    async def process(request: Request):
        found = _profile_for(profiles, request.headers.get("authorization", ""), usage)
        if found is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        name, p, job = found
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
        try:
            size = image_size(body)
        except ValueError:
            return JSONResponse({"error": "bad request"}, 400)
        langs = [x.strip() for x in request.headers.get("x-language", "").split(",") if x.strip()][:8]
        if busy.locked():
            return JSONResponse({"error": "busy"}, 503)
        async with busy:
            engine = p["engine"]
            call = {"feature": "layout", "profile": name, "model": p.get("model") or engine}
            try:
                if engine == "yolo-onnx":
                    answer, u = await asyncio.to_thread(run_yolo, p, body), {}
                else:
                    async with httpx.AsyncClient(timeout=timeout, transport=transport) as c:
                        answer, u = await (run_docling if engine == "docling-serve" else run_vlm)(c, p, body, ctype, size, langs)
            except upstream_auth.TokenError as e:  # never reached the model: no usage row
                return JSONResponse({"error": str(e)}, 502)
            except Exception as e:  # noqa: BLE001  BadUpstream or a failed call: the parser escalates
                usage.record(job, **call, outcome="error", unit="tokens" if engine == "openai-vlm" else "pages", input=0, output=0)
                return JSONResponse({"error": f"upstream failed ({type(e).__name__}{': ' + str(e) if isinstance(e, BadUpstream) else ''})"}, 502)
            if engine == "openai-vlm":
                usage.record(job, **call, outcome="ok", unit="tokens", input=u.get("prompt_tokens", 0), output=u.get("completion_tokens", 0))
            else:
                usage.record(job, **call, outcome="ok", unit="pages", input=1, output=0)
            answer["escalate_below"] = p["escalate_below"] if "escalate_below" in p else ESCALATE_BELOW[engine]
            answer["max_figure_members"] = int(p.get("max_figure_members") or MAX_FIGURE_MEMBERS[engine])
            return JSONResponse(answer)

    return Starlette(routes=[Route("/process", process, methods=["POST"]), Route("/health", lambda r: JSONResponse({"ok": True})), *usage.routes(profiles)])


def app_from_env() -> Starlette:
    e = os.environ
    return make_app(json.loads(e["LAYOUT_PROFILES"]), int(e.get("LAYOUT_MAX_BODY") or 10 << 20), int(e.get("LAYOUT_MAX_CONCURRENT") or 2), float(e.get("LAYOUT_TIMEOUT") or 120),
                    admin_token=e.get("LAYOUT_ADMIN_TOKEN") or None, max_calls=int(e.get("PARSE_USAGE_MAX_CALLS") or 20000))
