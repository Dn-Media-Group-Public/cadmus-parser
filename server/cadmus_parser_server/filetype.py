"""The one place that decides what a file is: libmagic on the bytes, refined by the extension of the name only where the bytes are generic."""

import mimetypes


# the bytes say "some container / some text", the name may say more (csv, json, md, html, a ZIP that is an .epub); extend by test case
GENERIC = {"application/octet-stream", "application/zip", "text/plain", "application/x-ole-storage", "application/x-empty", "inode/x-empty", "application/x-cfb",
           "application/x-ms-ooxml", "application/json", "text/xml", "application/xml"}


def detect(path: str, name: str | None = None) -> str:
    """Content type from the bytes (libmagic), refined by the extension only where the bytes are generic."""
    try:
        import magic  # part of the `files` extra; without it the name alone decides

        ctype = magic.from_file(path, mime=True)
    except Exception:  # noqa: BLE001  unreadable or no libmagic: fall back to the name
        ctype = "application/octet-stream"
    if ctype in GENERIC and name and (guess := mimetypes.guess_type(name)[0]):
        return guess
    return ctype
