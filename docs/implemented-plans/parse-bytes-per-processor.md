# Plan: per-processor size caps (`MAX_PARSE_BYTES_*`)

Supersedes [media-decode-max-bytes](media-decode-max-bytes.md) (a job option; not needed).

## Problem / Goal

`MAX_PARSE_BYTES` caps only the `files` processors; `media-decode` ignores it, so one huge video gets a full ffmpeg decode and a deadline kill fails the whole job.
Goal: more of the same: operator env vars, one per processor family, read at import, forwarded by the wrapper.

## Design

- New `MAX_PARSE_BYTES_FILES` for the `files` processors (`sizecap`, `archives`, `markdown`, `binary`, `html`, `apk`, `documents`).
- Media is split into three variables, `MAX_PARSE_BYTES_IMAGE`, `MAX_PARSE_BYTES_VIDEO` and `MAX_PARSE_BYTES_SOUND`, all for `media-decode`. `accepts` picks the variable from the file's type (the major part of its mime type: `image/*`, `video/*`, `audio/*`) and returns false over it, so the file falls through to `metadata` and `sizecap` as if it were not media (`0` = no cap). There is no combined `MAX_PARSE_BYTES_MEDIA`.
- `MAX_PARSE_BYTES` is kept as a fallback: each per-processor variable that is unset (or empty) takes the value of `MAX_PARSE_BYTES` if that is set. Resolution order per family: `MAX_PARSE_BYTES_<FAMILY>`, then `MAX_PARSE_BYTES`, then the built-in default. Existing deployments that set only `MAX_PARSE_BYTES` therefore keep working, but now also cap image, video and sound at that value; set the specific variable to override.
- Built-in defaults (when neither variable is set): `files` 50 MiB (unchanged), `image`, `video` and `sound` 2 GiB each (carried over from the single media cap; tune per type if wanted).
- A small shared helper (e.g. in `parse.py`) does the lookup, `_max_bytes(family, default)`, so both modules resolve it identically.
- `asr`, `caption` and `frame-ocr` get no variable of their own: they only act on `media-decode` output (chunks, units, `decode.json`), so a file `media-decode` skips never reaches them.
- Over a media cap the file is stored and gets metadata only; `sizecap` adds its note only if the size is also over `MAX_PARSE_BYTES_FILES`, so `media-decode` adds its own: `"<rel> not decoded (N bytes > MAX_PARSE_BYTES_VIDEO)"` (the name is the specific variable for the file's type, even when the value came from the `MAX_PARSE_BYTES` fallback).

## Implementation order

1. `parse.py`: add the `_max_bytes` helper, `MAX_PARSE_BYTES_FILES` via it, note text names the variable. `media_decode.py`: the three `MAX_PARSE_BYTES_{IMAGE,VIDEO,SOUND}` via the helper, per-type size check in `accepts`, note.
2. `wrapper.py` `PARSER_ENV`: keep `MAX_PARSE_BYTES`, add the four new names; `docker-compose.parse.yml`: same.
3. Tests: `test_wrapper.py` (env forwarding of all five), `test_parse.py` (resolution order: specific, fallback, default), `test_metadata.py` (monkeypatch the new name), new `test_media_decode.py` cases (per type: over, under, 0, fallback from `MAX_PARSE_BYTES`; one type's cap does not affect another).

## Files to change

| File | What changes |
|---|---|
| `server/cadmus_parser_server/parse.py` | `_max_bytes` helper, `MAX_PARSE_BYTES_FILES` |
| `server/cadmus_parser_server/media_decode.py` | `MAX_PARSE_BYTES_{IMAGE,VIDEO,SOUND}`, per-type cap in `accepts` |
| `server/cadmus_parser_server/wrapper.py`, `docker-compose.parse.yml` | env names |
| `server/tests/*` | as above |

## Docs to update

`docs/processors.md`, `docs/reference.md`, `docs/versioning.md` (new env vars, `MAX_PARSE_BYTES` kept as fallback: minor per its rules; note the behaviour change that `MAX_PARSE_BYTES` now also caps image, video and sound).
