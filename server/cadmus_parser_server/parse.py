"""The default processors (the `files` extra), one per kind of file / library: `metadata` (ExifTool, ffprobe, ...), `sizecap`, `archives`, `markdown`,
`binary` (msgpack, CBOR, BSON, plist, protobuf), `html` (trafilatura), `apk`, `documents` (markitdown and OCR: the fallback for everything else) and
`members` (keeps unpacked archive members). Ordered by `before`/`after`, an exclusive handler claims the file. They have no storage access and no
network: the wrapper moves the bytes. Settings are the options of the family (`options["files"]` of the job request): `base_url` (resolves links of
HTML inputs), `ctypes` ({input name: content type}). Archives are unpacked here, in the sandboxed process; the members go back through the worker
with `ctx.recurse` (they are parsed, never unpacked again)."""

import mimetypes
import os
import shutil

from cadmus_parser_server import extract, metadata, ocr

MAX_PARSE_BYTES = int(os.environ.get("MAX_PARSE_BYTES") or 50 * 1024 * 1024)  # stored files have no cap; parsing does
MAX_ARCHIVE_BYTES = int(os.environ.get("MAX_ARCHIVE_BYTES") or os.environ.get("MAX_ZIP_BYTES") or 4 * 1024**3)  # uncompressed, per archive
MAX_ARCHIVE_MEMBERS = int(os.environ.get("MAX_ARCHIVE_MEMBERS") or os.environ.get("MAX_ZIP_MEMBERS") or 50_000)


def _guess(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _terse(sink, name: str, md: str) -> None:
    if t := extract.terse(md):
        sink.put_bytes(f"terse/{name}", t.encode(), "text/markdown")


def _opts(ctx) -> dict:
    return ctx.options_of("files")


def _ctype(file, ctx) -> str:
    return (_opts(ctx).get("ctypes") or {}).get(file.rel) or _guess(file.name if file.depth else file.rel)


def _small(file) -> bool:
    return file.size <= MAX_PARSE_BYTES


def _is_html(file, ctype: str) -> bool:
    return "html" in ctype or file.rel.lower().endswith((".html", ".htm"))


def _read(file) -> bytes:
    with open(file.path, "rb") as f:
        return f.read()


class Metadata:
    """ExifTool, ffprobe, GDAL, OOXML, PDF history: `metadata/<rel>.yaml`; also says what the input is. First, for every file, whatever its size (headers, not bodies)."""
    name, top_dirs, before = "metadata", ("metadata",), ("sizecap", "archives", "markdown", "binary", "html", "apk", "documents")

    def accepts(self, file) -> bool:
        return True

    def process(self, sink, file, ctx) -> None:
        ctype, path = _ctype(file, ctx), file.rel
        sink.info(content_type=ctype, **({"size": file.size} if file.depth else {}))
        notes: list = []
        meta = metadata.extract_metadata(file.path, ctype, path, notes, tmp=ctx.work)
        for n in notes:
            sink.note(n)
        if meta:
            text, truncated = metadata.dump_yaml(meta)
            if truncated:
                sink.note(f"{path}: metadata truncated (> METADATA_MAX_BYTES)")
            sink.put_bytes(f"metadata/{path}.yaml", text.encode(), "application/yaml")


class SizeCap:
    """A file over MAX_PARSE_BYTES is stored and gets metadata only; the content processors do not accept it."""
    name, top_dirs, before, after = "sizecap", (), ("archives", "markdown", "binary", "html", "apk", "documents"), ("metadata",)

    def accepts(self, file) -> bool:
        return not _small(file)

    def process(self, sink, file, ctx) -> None:
        sink.note(f"{file.rel} not parsed ({file.size} bytes > MAX_PARSE_BYTES)")


class Archives:
    """zip, tar, 7z, rar, single-file gz/bz2/xz: unpacked into the work dir, every member handed back with `ctx.recurse` (members are never unpacked again,
    nor is an archive whose `<x>.d/` an earlier run stored)."""
    name, top_dirs, after, before = "archives", (), ("sizecap",), ("markdown", "binary", "html", "apk", "documents")

    def accepts(self, file) -> bool:  # cheap: the stored content type is only known to `process`
        return _small(file) and not file.depth and not file.derived and not os.path.isdir(file.path + ".d")

    def process(self, sink, file, ctx) -> None:
        ctype = _ctype(file, ctx)
        if not extract.archive_kind(ctype, file.rel):
            return
        try:
            with open(file.path, "rb") as af:
                members = extract.unpack_archive(af, file.rel, ctype, MAX_ARCHIVE_MEMBERS, MAX_ARCHIVE_BYTES, [])
                for name, f in members:
                    staged = os.path.join(ctx.work, "member" + os.path.splitext(name.lower())[1][:12])
                    with open(staged, "wb") as d:
                        shutil.copyfileobj(f, d)
                    ctx.recurse(staged, name)
            ctx.claim()
        except extract.ArchiveLimit as e:
            sink.note(str(e))
            ctx.claim()
        except extract.BadArchive:
            pass  # named like an archive but is not: the others parse it as any file


class Markdown:
    """Already Markdown: no parsed/, a terse outline from the file itself, and the input marked as text."""
    name, top_dirs, after, before = "markdown", ("terse",), ("sizecap", "archives"), ("documents",)

    def accepts(self, file) -> bool:
        return _small(file) and file.rel.lower().endswith((".md", ".markdown"))

    def process(self, sink, file, ctx) -> None:
        sink.info(meta={"text": True})
        _terse(sink, file.rel, _read(file).decode("utf-8", errors="replace"))
        ctx.claim()


class Binary:
    """msgpack, CBOR, BSON, plist, protobuf / gRPC-web: pretty-printed JSON at `parsed/<rel>.json`."""
    name, top_dirs, after, before = "binary", ("parsed",), ("sizecap", "archives", "markdown"), ("documents",)

    def accepts(self, file) -> bool:
        return _small(file)

    def process(self, sink, file, ctx) -> None:
        ctype, raw = _ctype(file, ctx), _read(file)
        if not (kind := extract.binary_kind(ctype, file.rel, raw)):
            return
        ctx.claim()
        try:
            text = extract.binary_to_json(raw, kind, ctype)
        except Exception as e:  # noqa: BLE001  undecodable: noted, never fails the job
            return sink.note(f"{file.rel} not parsed ({type(e).__name__}: {e})")
        sink.put_bytes(f"parsed/{file.rel}.json", text.encode(), "application/json")


class Apk:
    """Nothing to parse: stored for installing."""
    name, top_dirs, after, before = "apk", (), ("sizecap", "archives", "markdown", "binary"), ("documents",)

    def accepts(self, file) -> bool:
        return _small(file) and file.rel.lower().endswith(".apk")

    def process(self, sink, file, ctx) -> None:
        ctx.claim()


class Html:
    """HTML: main content to Markdown (trafilatura and markitdown), a simplified main-content guess, links made absolute from `base_url`."""
    name, top_dirs, after, before = "html", ("parsed", "simplified", "terse"), ("sizecap", "archives", "markdown", "binary", "apk"), ("documents",)

    def accepts(self, file) -> bool:
        return _small(file)

    def process(self, sink, file, ctx) -> None:
        if not _is_html(file, _ctype(file, ctx)):
            return
        raw, path, base_url = _read(file), file.rel, _opts(ctx).get("base_url") or ""
        ctx.claim()
        if base_url:
            raw = extract.absolutize(raw, base_url)  # parsed/ and simplified/ get absolute links; the input is stored untouched
        try:
            text = extract.html_to_markdown(raw)
        except Exception as e:  # noqa: BLE001  one unparseable file never fails the job
            sink.note(f"{path} not parsed ({type(e).__name__}: {e})")
            return
        if not text.strip():
            return
        sink.put_bytes(f"parsed/{path}.md", text.encode(), "text/markdown")
        if simple := extract.html_to_simplified(raw):
            sink.put_bytes(f"simplified/{path}.md", simple.encode(), "text/markdown")
        _terse(sink, f"{path}.md", text)


class Documents:
    """Everything else (office, PDF, images, text ...): markitdown, with OCR for images and scanned PDF pages; the fallback, after every other handler."""
    name, top_dirs, after = "documents", ("parsed", "terse"), ("sizecap", "archives", "markdown", "binary", "apk", "html")

    def accepts(self, file) -> bool:
        return _small(file)

    def process(self, sink, file, ctx) -> None:
        raw, path, ctype = _read(file), file.rel, _ctype(file, ctx)
        try:
            text = extract.file_to_markdown(raw, path)
        except Exception as e:  # noqa: BLE001
            sink.note(f"{path} not parsed ({type(e).__name__}: {e})")
            text = ""
        text = ocr.maybe_ocr(raw, path, ctype, text, sink.note)
        if not text.strip():
            if b"\0" not in raw[:1024]:  # ponytail: NUL sniff; text nobody converted is still returned inline from the input
                sink.info(meta={"text": True})
            return
        sink.put_bytes(f"parsed/{path}.md", text.encode(), "text/markdown")
        _terse(sink, f"{path}.md", text)


class Members:
    """An archive member unpacked in this run is also kept, as an output of its own entry (`original/<archive>.d/…`). First, on a copy, so that a handler
    that claims the file does not stop it and the others still read the original."""
    name, top_dirs, before = "members", ("original",), ("metadata", "sizecap", "archives", "markdown", "binary", "apk", "html", "documents")

    def accepts(self, file) -> bool:
        return bool(file.depth) and file.path.startswith(os.path.dirname(file.path) + os.sep) and os.path.basename(file.path).startswith("member")

    def process(self, sink, file, ctx) -> None:
        if not file.path.startswith(ctx.work + os.sep):
            return
        keep = os.path.join(ctx.work, "keep" + os.path.splitext(file.path)[1])
        shutil.copyfile(file.path, keep)
        sink.put_file(f"original/{file.rel}", keep, _ctype(file, ctx))


PROCESSORS = (Metadata, SizeCap, Archives, Markdown, Binary, Apk, Html, Documents, Members)
