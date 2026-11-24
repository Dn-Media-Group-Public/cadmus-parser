"""filetype.detect: bytes first, the name only where the bytes are generic."""

import io
import zipfile

from cadmus_parser_server.filetype import detect


def zipped(files: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


def at(tmp_path, name, data):
    p = tmp_path / "original"
    p.write_bytes(data)
    return str(p), name


def test_table(tmp_path):
    png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82'
    cases = [
        (zipped({"[Content_Types].xml": "x", "word/document.xml": "x"}), None, "wordprocessingml"),
        (zipped({"[Content_Types].xml": "x", "xl/workbook.xml": "x"}), None, "spreadsheetml"),
        (zipped({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": "x"}), None, "opendocument.text"),
        (zipped({"mimetype": "application/epub+zip", "a.xhtml": "x"}), None, "epub"),
        (zipped({"a.txt": "hi"}), None, "application/zip"),
        (zipped({"a.txt": "hi"}), "book.epub", "epub"),  # a zip the name calls an epub
        (png, "photo.jpg", "image/png"),  # the name never overrides self-identifying bytes
        (b"a,b\n1,2\n", "t.csv", "text/csv"),
        (b'{"a": 1}', "t.json", "json"),
        (b"hello world\n", None, "text/plain"),
        (b"ID3\x04\0\0\0\0\0\0\xff\xfb\x90\x00" + b"\0" * 400, None, "audio"),
        (b"\0\0\0\x18ftypisom\0\0\0\0isomiso2" + b"\0" * 32, None, "video"),
    ]
    for data, name, want in cases:
        path, _ = at(tmp_path, name, data)
        assert want in detect(path, name), (name, data[:12])


def test_parse_ctype_uses_detect_unless_ctypes_says_so(tmp_path):
    from types import SimpleNamespace as NS

    from cadmus_parser_server import parse
    p = tmp_path / "original"
    p.write_bytes(b"a,b\n1,2\n")
    f = NS(path=str(p), name="report.csv", rel="report.csv")
    assert parse._ctype(f, NS(options_of=lambda n: {})) == "text/csv"
    assert parse._ctype(f, NS(options_of=lambda n: {"ctypes": {"report.csv": "text/x-given"}})) == "text/x-given"
