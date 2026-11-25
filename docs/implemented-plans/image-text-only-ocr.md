# Plan: Images only get `parsed/*.md` when OCR finds text

> **Status: implemented.** Open questions resolved as suggested (skip markitdown for images; no LLM description; the `ocr <how>` note is the only marker).
> Written for the article-graph consumer ([Plan: Document body text](../../../../docs/plans/document-body.md), which assumes this is done).

## Problem / Goal

For an image, the `documents` processor (`server/cadmus_parser_server/parse.py`, `Documents.process`) first runs markitdown, whose output for an image is
only the file's EXIF metadata (`ImageSize: 1920x1440`, ...), then `ocr.maybe_ocr` (`ocr.py:92`) does `return out if out.strip() else text`: when OCR
finds nothing, the metadata text is written as `parsed/<path>.md`. Consumers read `parsed/*.md` as "the text in the image" and show metadata as text
(article-graph: a photo with no text gets `*Text:* ImageSize: 1920x1440`). The same metadata is already in `metadata/` (the `metadata` processor).

Goal: for an image, `parsed/<path>.md` (and `terse/`) exists **only if OCR produced text**.

## Design

- In `Documents.process`, for image types (`ocr.IMAGE_TYPES`, from `_ctype`) skip `extract.file_to_markdown`: the text is `""` and OCR alone decides.
- `maybe_ocr` keeps its signature; its image branch returns `out` (no fallback to markitdown text).
- No `.md` is written for an empty result (the existing `if not text.strip(): return` path; the `meta={"text": True}` inline-text hint is only for non-binary input, which an image is not).
- When OCR is unavailable (`ImportError`) or fails, the note is recorded as today and no `.md` is written (previously the metadata text was).
- Non-image files (PDF, office, text) are unchanged.

## Open questions

1. Skip markitdown for images entirely (suggested), or only drop the `else text` fallback? The second leaves metadata as text when OCR is unavailable/failed.
2. Should markitdown's optional image description (it can call an LLM when configured) be kept? Cadmus does not configure one, so suggested: no.
3. Is "no `parsed/*.md`" an acceptable output for an image to every client (`client.md`, `reference.md`), or should an explicit marker (e.g. a note `<rel>: no text found`) be emitted? Suggested: the existing `ocr <how>` note is enough.

## Implementation order

1. Decide the open questions.
2. `parse.py` and `ocr.py` changes above.
3. Tests: image with text -> `parsed/*.md`; image without text -> no `parsed/`/`terse/` entry, metadata still in `metadata/`; OCR unavailable -> note, no `.md`; PDF/docx unchanged.
4. Docs; move this plan to `docs/implemented-plans/` and update both indexes.

## Files to change

| File | Change |
|---|---|
| `server/cadmus_parser_server/parse.py` | `Documents.process`: no markitdown for image types |
| `server/cadmus_parser_server/ocr.py` | `maybe_ocr` image branch: no fallback to markitdown text |
| server tests | cases in step 3 |

## Docs to update

`docs/processors.md` (the `documents` description: images get markdown only from OCR), `docs/reference.md` if it lists `parsed/` for images, `docs/plans/index.md`, `docs/implemented-plans/index.md`.
