# Plan: Frame OCR

> **Status: draft.** Depends on [processor-handoff](01-processor-handoff.md) and [media-decode](02-media-decode.md).

## Problem / Goal

Cadmus OCRs images and scanned PDF pages (Tesseract first, a remote multimodal model through `ocr-sidecar` as fallback) but not the frames of a video. Text in
video (signs, lower-thirds, slides, documents on screen) is lost. [media-decode](02-media-decode.md) writes frame files cheap to read, which is what this plan consumes.

Goal: a `frame-ocr` processor that runs the existing OCR engine over a video's frames and writes timestamped text.

## Design

- Processor `frame-ocr` (`top_dirs = ("frame-ocr",)`, after `media-decode`), for video only. It reuses the `ocr` processor's engine code and the existing `ocr` feature/
  sidecar (so no new sidecar, token or profile kind).
- **Which frames:** option `pass` names one entry of `media-decode`'s `frame_passes` (default: the highest-resolution pass with fps <= 1, else the highest-resolution
  pass). MJPEG all-intra passes (fps < 2) are read frame by frame; H.264 passes are decoded sequentially.
- **Deduplicating text across time.** Text on screen persists for seconds; running OCR on every frame produces the same text many times. The processor skips a frame
  that is perceptually near-identical to the previous OCRed one (cheap downscaled diff, option `change_threshold`), and merges consecutive identical or near-identical
  results into one entry with `t_start`/`t_end`.
- **Output** `frame-ocr/<file.rel>/ocr.json`: `[{"t_start": 12.0, "t_end": 18.0, "text": "...", "conf": 71, "engine": "tesseract|remote"}]`, plus a note when the frame
  cap (`max_frames`, default 5000) was reached.
- Per-frame Tesseract uses the OCR settings that exist today (`OCR_LANGS`, `OCR_MIN_CONF`, `OCR_MIN_CHARS`, ...); the remote fallback goes through `ocr-sidecar` and so
  through [sidecar-usage-reporting](03-sidecar-usage-reporting.md) once the OCR sidecar adopts it.

## Open questions

1. How a consumer places this text in a document body is the consumer's decision (for article-graph, see its parsing plan); this plan only produces the data.
2. Whether images embedded in documents (already OCRed by `documents`) and still frames of video share code beyond the engine; proposed: the engine only.

## Implementation order

1. Frame iterator over a frame-store file; change detection and run-merging as pure functions with tests.
2. Processor, tests on a synthetic video with text appearing and disappearing.
3. Docs.

## Files to change

| File | Change |
|---|---|
| `server/cadmus_parser_server/frame_ocr.py` (new) | processor |
| `server/cadmus_parser_server/ocr.py` | engine callable shared with the processor |
| `server/pyproject.toml` | entry point |

## Docs to update

`README.md`, `docs/reference.md` (options, `ocr.json`), `docs/processors.md`.
