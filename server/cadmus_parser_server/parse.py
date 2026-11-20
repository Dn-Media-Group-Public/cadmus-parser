"""The default `files` processor: markitdown, trafilatura, archive unpacking, binary decoders, file metadata, OCR. It sees one file at a time
(`accepts` is true for everything, `process` writes through the sink) and has no storage access and no network: the wrapper moves the bytes.
Options (`options["files"]` of the job request): `base_url` (resolves links of HTML inputs), `ctypes` ({input name: content type}).
Archives are unpacked here, in the sandboxed process; the members go back through the worker with `ctx.recurse` (they are parsed, never
unpacked again)."""

import mimetypes
import os
import shutil

from cadmus_parser_server import extract, metadata, ocr

MAX_PARSE_BYTES = int(os.environ.get("MAX_PARSE_BYTES") or 50 * 1024 * 1024)  # stored files have no cap; parsing does
MAX_ARCHIVE_BYTES = int(os.environ.get("MAX_ARCHIVE_BYTES") or os.environ.get("MAX_ZIP_BYTES") or 4 * 1024**3)  # uncompressed, per archive
MAX_ARCHIVE_MEMBERS = int(os.environ.get("MAX_ARCHIVE_MEMBERS") or os.environ.get("MAX_ZIP_MEMBERS") or 50_000)


def _ctype(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _terse(sink, name: str, md: str) -> None:
    if t := extract.terse(md):
        sink.put_bytes(f"terse/{name}", t.encode(), "text/markdown")


def _convert(sink, path: str, raw: bytes, ctype: str, base_url: str) -> None:
    if path.lower().endswith((".md", ".markdown")):  # already Markdown: no parsed/, terse from the original
        sink.info(meta={"text": True})
        return _terse(sink, path, raw.decode("utf-8", errors="replace"))
    if bkind := extract.binary_kind(ctype, path, raw):
        try:
            text = extract.binary_to_json(raw, bkind, ctype)
        except Exception as e:  # noqa: BLE001  undecodable: noted, never fails the job
            return sink.note(f"{path} not parsed ({type(e).__name__}: {e})")
        return sink.put_bytes(f"parsed/{path}.json", text.encode(), "application/json")
    if path.lower().endswith(".apk"):  # nothing to parse: stored for device_install_apk
        return
    is_html = "html" in ctype or path.lower().endswith((".html", ".htm"))
    if is_html and base_url:
        raw = extract.absolutize(raw, base_url)  # parsed/ and simplified/ get absolute links; the input is stored untouched
    try:
        text = extract.html_to_markdown(raw) if is_html else extract.file_to_markdown(raw, path)
    except Exception as e:  # noqa: BLE001  one unparseable file never fails the job
        sink.note(f"{path} not parsed ({type(e).__name__}: {e})")
        text = ""
    if not is_html:
        text = ocr.maybe_ocr(raw, path, ctype, text, sink.note)
    if not text.strip():
        if not is_html and b"\0" not in raw[:1024]:  # ponytail: NUL sniff; text nobody converted is still returned inline from the input
            sink.info(meta={"text": True})
        return
    sink.put_bytes(f"parsed/{path}.md", text.encode(), "text/markdown")
    if is_html and (simple := extract.html_to_simplified(raw)):
        sink.put_bytes(f"simplified/{path}.md", simple.encode(), "text/markdown")
    _terse(sink, f"{path}.md", text)


def parse_file(sink, ctx, file, ctype: str, base_url: str = "") -> None:
    """Parse the file; `file.rel` names the outputs (`parsed/<rel>.md`), an unpacked member goes back through `ctx.recurse`."""
    path, src = file.rel, file.path
    size = os.path.getsize(src)
    sink.info(content_type=ctype, **({"size": size} if file.depth else {}))
    meta_notes: list = []
    meta = metadata.extract_metadata(src, ctype, path, meta_notes, tmp=ctx.work)  # before the size check: headers, not whole files
    for n in meta_notes:
        sink.note(n)
    if meta:
        text, truncated = metadata.dump_yaml(meta)
        if truncated:
            sink.note(f"{path}: metadata truncated (> METADATA_MAX_BYTES)")
        sink.put_bytes(f"metadata/{path}.yaml", text.encode(), "application/yaml")
    if size > MAX_PARSE_BYTES:
        return sink.note(f"{path} not parsed ({size} bytes > MAX_PARSE_BYTES)")
    if not file.depth and not file.derived and not os.path.isdir(src + ".d") and extract.archive_kind(ctype, path):  # a `<x>.d/` next to it: unpacked before
        try:
            with open(src, "rb") as af:
                members = extract.unpack_archive(af, path, ctype, MAX_ARCHIVE_MEMBERS, MAX_ARCHIVE_BYTES, [])
                for name, f in members:
                    _member(ctx, f, name, file)
            return
        except extract.ArchiveLimit as e:
            return sink.note(str(e))
        except extract.BadArchive:
            pass  # named like an archive but is not: parse it as any file
    with open(src, "rb") as f:
        _convert(sink, path, f.read(), ctype, base_url)


def _member(ctx, f, name: str, archive) -> None:
    """Stage a member in the work dir and hand it back to the worker as a member of the archive."""
    staged = os.path.join(ctx.work, "member" + os.path.splitext(name.lower())[1][:12])
    with open(staged, "wb") as d:
        shutil.copyfileobj(f, d)
    ctx.recurse(staged, name)


class Files:
    name = "files"
    top_dirs = ("original", "parsed", "simplified", "terse", "metadata")  # original/: unpacked archive members, `original/<archive>.d/…`

    def accepts(self, file) -> bool:
        return True

    def process(self, sink, file, ctx) -> None:
        ctype = (ctx.options.get("ctypes") or {}).get(file.rel) or _ctype(file.name if file.depth else file.rel)
        parse_file(sink, ctx, file, ctype, ctx.options.get("base_url") or "")
        if file.path.startswith(ctx.work + os.sep) and os.path.exists(file.path):  # a member unpacked now is also kept, as an output of its own entry
            sink.put_file(f"original/{file.rel}", file.path, ctype)
