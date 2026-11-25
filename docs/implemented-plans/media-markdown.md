# Plan: media-markdown — one combined markdown for images, video and audio

## Problem / Goal

ASR (`asr/<rel>/transcript.json`), captions (`caption/<rel>/captions.json`) and frame OCR (`frame-ocr/<rel>/ocr.json`) are returned only as structured JSON. A video has no `parsed/<path>.md`, so a consumer that reads parsed text gets nothing for it. (Today `documents` would run markitdown on video/audio; it yields little or nothing useful.) Goal: a single human/LLM-readable, time-interleaved markdown at `parsed/<path>.md` for images, video and audio, built from whichever of the three features ran and succeeded.

## Design (decided)

- **New processor `media-markdown`** (new module `server/cadmus_parser_server/media_markdown.py`, entry point in `server/pyproject.toml`). `after = ("media-decode", "asr-split", "caption-split", "frame-ocr")`, `before = HANDLERS` (from `asr.py`), `top_dirs = ("parsed", "terse")`. It owns media: for a file media-decode decoded (`decode.json` kind `image` / `video` / `audio`) it writes `parsed/<rel>.md` (+ ) and **claims the file**, so `documents` never sees it. No `decode.json` (not media) -> return without claiming.
- **Image OCR moves into it.** For `kind == image` it calls `ocr.maybe_ocr` exactly as `documents` does today (Tesseract, remote OCR when the job asked for `ocr`), so image OCR behaviour is unchanged; `documents` keeps handling everything that is not media.
- **Inputs are scratch copies.** `asr-split`, `caption-split` and `frame-ocr` keep writing their JSON outputs **at their current paths** (no move), and additionally write the same JSON into their own `ctx.dir()`. `media-markdown` reads those from `ctx.work/<processor>/<rel>/`. A feature that did not run, raised, or produced nothing leaves no scratch file; its parts are simply omitted. The markdown is written if **any** part exists; if none do (and it is not an image with OCR text), no `parsed/` file is written and the file is still claimed.
- **Layout (revised after the first real run): a flat, time-ordered list of paragraphs, no headings, text only.** Speech `` `0:06` **Speaker**: text ``, caption `` `0:08` _SCENE_: text ``, frame OCR `` `0:05` _ON SCREEN_: text ``; ties: scene, on screen, speech. Image: caption, then OCR text, no times. Skipped/blocked caption units and failed chunks produce no text. No `terse/` output (it keeps only headings and links). No options.
- Keyframes, `media/<rel>/…`, and the three JSON outputs are unchanged.

## Implementation order

1. Pure helpers in `media_markdown.py`: `build_markdown(kind, captions, ocr_entries, segments, image_text)`, tested on synthetic data.
2. `asr.py`, `caption.py`, `frame_ocr.py`: also write their result JSON to `ctx.dir()`.
3. `MediaMarkdown` processor: read decode.json + scratch files, image OCR via `ocr.maybe_ocr`, `put_bytes` + `_terse`, claim.
4. Register entry point; check ordering (`processors.order`) has no cycle with `documents` and the handler list.
5. Tests: only ASR; only OCR; only caption; all three; none (claims, no output); image with OCR text only; image with caption + OCR; non-media file not claimed (falls to `documents`).
6. Docs; move plan to implemented-plans.

## Verify during implementation

- That a raising `asr-split` ("no text from any chunk") does not stop `media-markdown` from running for the other parts of the same file.
- Whether `documents` has existing tests that cover images and must be moved.

## Files to change

| File | What |
|---|---|
| `server/cadmus_parser_server/media_markdown.py` | new: helpers + `MediaMarkdown` |
| `server/cadmus_parser_server/asr.py`, `caption.py`, `frame_ocr.py` | also write scratch JSON in `ctx.dir()` |
| `server/cadmus_parser_server/parse.py` | `documents`: image OCR branch left unreachable for media (remove if dead after the change) |
| `server/pyproject.toml` | entry point `media-markdown` |
| `server/tests/test_media_markdown.py` (+ image tests moved/adjusted) | new tests |

## Docs to update

`docs/reference.md` (new `parsed/` output for media, layout, option), `docs/processors.md` (new processor, ordering, scratch results), `docs/implemented-plans/index.md`, `README.md` if it lists outputs.
