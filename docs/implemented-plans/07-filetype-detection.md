# Plan: file type detection with python-magic

## Problem / Goal

Cadmus decides what a file is in four hand-written places, each with its own short magic-byte table: `parse._guess`/`_ctype` (extension only, via `mimetypes`),
`limits.MAGIC`, `media_decode.sniff`, and the consumer's `ctypes` option. A single-object input is named after its object, so a consumer that stores its
bytes under an opaque key (e.g. `<sha256>/original`) gives cadmus a file called `original` with no extension: `.docx`, `.xlsx`, audio and video are
then labelled `application/octet-stream`, and exiftool reports a bare ZIP.

Goal: one detector that uses **both** the bytes (libmagic through `python-magic`) and the file's real name (`mimetypes`). The consumer gives cadmus the real
name the usual way, by naming the input's file (a prefix input holding one file, or a single object with a real name): cadmus needs no new request field, and
the name is real for every processor, not only the detector.

## Design

### Detection

New `cadmus_parser_server/filetype.py`, the only place that knows about types:

```python
def detect(path: str, name: str | None = None) -> str:
    """Content type from the bytes (libmagic), refined by the extension only where the bytes are generic."""
```

- `magic.from_file(path, mime=True)` on the on-disk input (whole-file detection, so OOXML/ODF/EPUB inside a ZIP are recognised, which a 16-byte sniff cannot do).
- If libmagic returns a generic type (`application/octet-stream`, `application/zip`, `text/plain`, `application/x-ole-storage`, ...) and the name has an extension,
  `mimetypes.guess_type(name)` decides (csv, json, md, html, a ZIP that is really an `.epub`). Otherwise the bytes win: a name never overrides self-identifying
  content (no `.jpg` that is really a PDF).
- An explicit `options.files.ctypes[rel]` still wins over both (the caller said so).
- `name` is `file.name` for every file, whatever its depth (a prefix input's files and archive members already carry real names); the `file.depth` branch of
  `parse._ctype` goes.
- Replaces `parse._guess`, `limits.MAGIC` (`classify` reads the major type/`application/zip` of `detect`) and `media_decode.sniff` (image/video/audio is the
  major type of `detect`; the audio-only-video case is still settled at probe). The comment "never the name: an input may be called `original`" goes away with the premise.

### Packaging

`python-magic` goes in the `files` extra of `server/pyproject.toml` (pip); `libmagic1` (with its magic database) is installed in the parser image with the other apt
packages (`server/Dockerfile`, `docs/parser-image.md`). `detect` runs in the processor process, like the other file readers.

## Implementation order

1. Image: `libmagic1` in the Dockerfile and `docs/parser-image.md`; `python-magic` in the `files` extra.
2. `filetype.py` + tests (table of real small files: docx, xlsx, odt, epub, zip, png-named-`.jpg`, csv, json, text, mp3, mp4, ogg audio vs video).
3. Switch `parse.py`, `limits.py`, `media_decode.py` to `detect`; delete the old tables.
4. Test: a prefix input with one named file (`report.docx`) gets its real type, outputs are named after it (`parsed/report.docx.md`).
5. Docs.

## Files to change

| File | Change |
|---|---|
| `server/cadmus_parser_server/filetype.py` | new: `detect` |
| `server/cadmus_parser_server/parse.py` | `_ctype` calls `detect`; `_guess` removed |
| `server/cadmus_parser_server/limits.py` | `MAGIC` removed; `classify` from `detect` |
| `server/cadmus_parser_server/media_decode.py` | `sniff` removed |
| `server/pyproject.toml`, `server/Dockerfile` | `python-magic`, `libmagic1` |
| `server/tests/` | detection table, named-file input test |

## Docs to update

`docs/processors.md` (content type detection), `docs/parser-image.md` (`libmagic1`), `docs/security.md` (libmagic parses untrusted bytes in the sandboxed
process, like exiftool/ffmpeg), `docs/plans/index.md`.

## Open questions

1. The "generic" set that lets the extension decide: start from the list above and extend by test case, or make it an operator setting? Proposal: a module constant.
