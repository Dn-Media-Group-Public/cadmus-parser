# Plan: Caption (image-to-text)

> **Status: draft.** Depends on [processor-handoff](01-processor-handoff.md), [media-decode](../implemented-plans/02-media-decode.md) and [sidecar-usage-reporting](../implemented-plans/03-sidecar-usage-reporting.md); shares `modelcheck.py` and the primary/fallback pattern with [asr](../implemented-plans/04-asr.md).

## Problem / Goal

Images and video are not searchable text. A vision model can describe them, but the parser must not hold its key, and its answers need validation and a fallback like [asr](../implemented-plans/04-asr.md).

Goal: a `caption-sidecar` (one call to one vision model, holds the credentials) and processors that make each captionable unit a **file** and caption it with a primary and a
fallback processor, producing `caption/<file.rel>/captions.json`.

## Design

- **Features:** `caption` (`CAPTION_PROFILES`) and `caption-fallback` (`CAPTION_FALLBACK_PROFILES`) in the wrapper's `FEATURES`; a job asks for both profiles
  (they may share the sidecar service). Profile: base URL, auth, model, prompt.
- **Sidecar** (`POST /process`): for an image, the image bytes; for video, a multipart body of up to 20 keyframes interleaved with their timestamps (`X-Frame-Times`).
  Returns the raw text plus finish/block status. No retry, validation or fallback. Frames are scaled down by the processor to `max_side` (default 1536) before sending.
- **Processors** (`top_dirs = ("caption",)`), cadmus's priority order doing the fallback:
  1. **`caption-split`** (after `media-decode`; only when the job asked for `caption`): an image is one unit, passed to `ctx.recurse(..., synthetic=True)` like the video windows so the merge is uniform (its `web.png` if the original is not a web format); for
     video it writes **window files** to `ctx.work`, each a JSON manifest (`window-0003.json`: the keyframe paths and times of a window of 20 consecutive keyframes) and
     calls `ctx.recurse(..., synthetic=True)` on each (windows are not listed in `index.json`). After the last member it writes `captions.json` from the member results; **`captions.json` is the only output of the feature** (chunk and window processors write none).
  2. **`caption-primary`**: accepts the image or window files; one call to the `caption` profile, then validation (shared `modelcheck.py`: block, truncation, empty,
     repetition loop); retries once on `invalid` (not on `blocked`); on success keeps the result in its `ctx.dir()` (a scratch file, not an output) and claims; otherwise claims nothing.
  3. **`caption-fallback`** (after primary): the same against the `caption-fallback` profile for what is unclaimed.
  4. **`caption-skip`** (after fallback): a unit still unclaimed keeps an `outcome: "blocked"` or `"invalid"` result without text; it is **left out and noted**, it does not fail
     the job. (Unlike ASR there is no "degraded" text worth keeping.)
- The caption prompt allows Markdown (emphasis) but not raw HTML.
- **Output** `captions.json`:

```json
{"kind": "image|video",
 "captions": [{"t_start": 0.0, "t_end": 94.2, "text": "...", "outcome": "ok|blocked|invalid", "role": "primary|fallback", "model": "...", "frames": [0, 1, 2]}]}
```

An image has one entry with `t_start = t_end = 0`.

## Open questions

1. The worker mechanics (members, results, handoff, parallel members) are specified in [processor-handoff](01-processor-handoff.md).
2. Prompt text and window size are defaults to tune on real material; they are profile and job options, not code.
3. Whether OCR text of the same frames is merged into the caption call or kept in [frame-ocr](06-frame-ocr.md) only. Proposed: kept separate.

## Implementation order

1. [processor-handoff](01-processor-handoff.md) and `modelcheck.py` (from [asr](../implemented-plans/04-asr.md)).
2. `caption-sidecar/` (image and window requests, usage), tests with a fake upstream.
3. Processors: window building, scaling, primary/fallback/skip; tests on synthetic keyframes.
4. Wrapper `FEATURES`, overlay (`--caption`), stack file service; docs.

## Files to change

| File | Change |
|---|---|
| `caption-sidecar/` (new) | sidecar |
| `server/cadmus_parser_server/caption.py` (new) | four processors |
| `server/cadmus_parser_server/wrapper.py` | `FEATURES["caption"] = "CAPTION"`, `FEATURES["caption-fallback"] = "CAPTION_FALLBACK"` |
| `server/cadmus_parser_server/overlay.py`, `docker-compose.parse.yml` | `--caption`, service, network |
| `server/pyproject.toml` | four entry points |

## Docs to update

`README.md`, `docs/reference.md` (features, env vars, `captions.json`), `docs/sidecar-protocol.md`, `docs/processors.md`, `docs/quickstart.md`.
