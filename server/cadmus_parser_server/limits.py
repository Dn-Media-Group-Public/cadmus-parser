"""Per-class limits (docs: PARSE_LIMITS). The wrapper classifies every pulled file by extension, then by magic number, and runs the
parser under the per-limit maximum over the classes present. Only the operator sets numbers; every built-in default is finite."""

import mimetypes
import os

CLASSES = ("video", "audio", "image", "document", "archive", "other")
NAMES = ("deadline_s", "ram_mib", "fsize_mib", "input_mib", "output_mib", "nproc", "nofile")  # 0 = unlimited (only when the operator says so)
BASE = {"deadline_s": 600, "ram_mib": 6144, "fsize_mib": 4096, "input_mib": 50, "output_mib": 4096, "nproc": 1024, "nofile": 1024}
BUILTIN = {
    "video": {"deadline_s": 3600, "ram_mib": 16384, "fsize_mib": 16384, "input_mib": 20480, "output_mib": 20480},
    "audio": {"deadline_s": 1800, "ram_mib": 8192, "fsize_mib": 8192, "input_mib": 4096, "output_mib": 8192},
    "archive": {"deadline_s": 1800, "ram_mib": 8192, "fsize_mib": 4096, "input_mib": 4096, "output_mib": 8192},
}
EXT = {
    "video": ".mp4 .m4v .mov .mkv .webm .avi .wmv .flv .mpg .mpeg .3gp .ts .m2ts .ogv",
    "audio": ".mp3 .wav .flac .ogg .oga .opus .m4a .aac .wma .aiff .amr",
    "image": ".png .jpg .jpeg .gif .webp .bmp .tif .tiff .heic .heif .svg .ico .jp2 .psd",
    "archive": ".zip .tar .gz .tgz .bz2 .tbz2 .xz .txz .zst .7z .rar .cab",
    "document": ".pdf .doc .docx .odt .rtf .xls .xlsx .ods .ppt .pptx .odp .txt .md .markdown .html .htm .csv .json .xml .epub",
}
EXT_CLASS = {e: c for c, es in EXT.items() for e in es.split()}
MAGIC = [(b"\x89PNG", "image"), (b"\xff\xd8\xff", "image"), (b"GIF8", "image"), (b"%PDF", "document"), (b"PK\x03\x04", "archive"),
         (b"\x1f\x8b", "archive"), (b"BZh", "archive"), (b"\xfd7zXZ\x00", "archive"), (b"7z\xbc\xaf\x27\x1c", "archive"), (b"Rar!", "archive"),
         (b"ID3", "audio"), (b"fLaC", "audio"), (b"OggS", "audio"), (b"\x1aE\xdf\xa3", "video"), (b"FLV", "video"), (b"\xd0\xcf\x11\xe0", "document")]


def classify(path: str, name: str | None = None) -> str:
    """Class of the file at `path`: by the extension of `name` (default: the path), else by its first 16 bytes. The wrapper decodes nothing."""
    ext = os.path.splitext((name or path).lower())[1]
    if ext in EXT_CLASS:
        return EXT_CLASS[ext]
    if (guess := (mimetypes.guess_type(name or path)[0] or "").split("/")[0]) in ("video", "audio", "image"):
        return guess
    with open(path, "rb") as f:
        head = f.read(16)
    if head[4:8] == b"ftyp":
        return "video"
    if head[:4] == b"RIFF":
        return {b"WAVE": "audio", b"AVI ": "video", b"WEBP": "image"}.get(head[8:12], "other")
    return next((c for m, c in MAGIC if head.startswith(m)), "other")


def resolve(overrides: dict | None) -> dict:
    """{class: {limit: number}}: built-in of the class, overridden by `default`, overridden by the class. Raises ValueError on a bad key or value."""
    ov = overrides or {}
    if not isinstance(ov, dict) or set(ov) - {"default", *CLASSES}:
        raise ValueError(f"PARSE_LIMITS keys must be among default, {', '.join(CLASSES)}")
    for cls, lim in ov.items():
        if not isinstance(lim, dict) or set(lim) - set(NAMES) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in lim.values()):
            raise ValueError(f"PARSE_LIMITS[{cls}]: names must be among {', '.join(NAMES)}, values numbers >= 0")
    return {c: {**BASE, **BUILTIN.get(c, {}), **ov.get("default", {}), **ov.get(c, {})} for c in CLASSES}


def merge(table: dict, classes) -> dict:
    """The job's limit set: per limit the maximum over the classes present, 0 (unlimited) counting as the largest."""
    sets = [table[c] for c in classes] or [table["other"]]
    return {n: 0 if any(s[n] == 0 for s in sets) else max(s[n] for s in sets) for n in NAMES}


def ceiling(table: dict, name: str):
    """The operator's ceiling of one limit: the largest over all classes (0 = unlimited)."""
    return merge(table, CLASSES)[name]


def mib(n) -> int:
    return int(n * (1 << 20))
