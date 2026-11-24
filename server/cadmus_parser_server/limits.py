"""Per-class limits (docs: PARSE_LIMITS). The wrapper classifies every pulled file by its type (`filetype.detect`), and runs the
parser under the per-limit maximum over the classes present. Only the operator sets numbers; every built-in default is finite."""

from cadmus_parser_server.filetype import detect

CLASSES = ("video", "audio", "image", "document", "archive", "other")
NAMES = ("deadline_s", "ram_mib", "fsize_mib", "input_mib", "output_mib", "nproc", "nofile")  # 0 = unlimited (only when the operator says so)
BASE = {"deadline_s": 600, "ram_mib": 6144, "fsize_mib": 4096, "input_mib": 50, "output_mib": 4096, "nproc": 1024, "nofile": 1024}
BUILTIN = {
    "video": {"deadline_s": 3600, "ram_mib": 16384, "fsize_mib": 16384, "input_mib": 20480, "output_mib": 20480},
    "audio": {"deadline_s": 1800, "ram_mib": 8192, "fsize_mib": 8192, "input_mib": 4096, "output_mib": 8192},
    "archive": {"deadline_s": 1800, "ram_mib": 8192, "fsize_mib": 4096, "input_mib": 4096, "output_mib": 8192},
}
DOCUMENT = {"application/pdf", "application/msword", "application/rtf", "application/json", "application/xml", "application/epub+zip", "application/x-ole-storage"}
ARCHIVE = {"application/zip", "application/gzip", "application/x-bzip2", "application/x-xz", "application/x-7z-compressed", "application/vnd.rar",
           "application/x-rar", "application/x-tar", "application/zstd", "application/vnd.ms-cab-compressed"}


def classify(path: str, name: str | None = None) -> str:
    """Class of the file at `path` by `detect` (its bytes, refined by the name). The wrapper decodes nothing."""
    ctype = detect(path, name or path)
    if (major := ctype.split("/")[0]) in ("video", "audio", "image"):
        return major
    if ctype in ARCHIVE:
        return "archive"
    if major == "text" or ctype in DOCUMENT or ctype.startswith(("application/vnd.openxmlformats", "application/vnd.oasis.opendocument", "application/vnd.ms-")):
        return "document"
    return "other"


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
