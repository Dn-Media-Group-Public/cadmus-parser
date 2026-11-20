# Plan: Media decode

> **Status: draft.** Depends on [processor-handoff](processor-handoff.md). Written for the article-graph consumer; general to cadmus. Open items are under "Open questions".

## Problem / Goal

Cadmus reads metadata (ExifTool, ffprobe) from audio, video and images but produces no derived media: no thumbnail, no keyframes, no normalised audio track, no
browser-playable rendition, no resampled frames for per-frame analysis. A consumer that wants to show media in a browser, caption it, transcribe it or run OCR
on its frames has to decode the original itself, which is the expensive step (about 0.124 CPU-s per video second, single thread, for a decode with several outputs)
and is exactly the untrusted-file work cadmus exists to sandbox.

Goal: one new default processor, `media-decode`, that decodes an image, video or audio file once and writes every derived artefact below.

## Design

### One processor, one ffmpeg invocation

`media-decode` (`top_dirs = ("media",)`, runs after `metadata`, before processors that consume its outputs) accepts files whose magic bytes say image, video or audio
(not the file name: a consumer may name its input `original` with no extension). For video it runs **one `ffmpeg` invocation with several outputs**, so the
source is decoded once; Python then post-processes the candidates. Pure extraction and format conversion, no analysis. All output is below
`media/<file.rel>/`.

| Output | Applies to | Details |
|---|---|---|
| `thumb.jpg` | image, video | Downscaled still (default longest side 320 px; for video a frame near 10 % of the duration). Not made for audio (embedded cover art is ignored). |
| `keyframes/<n>.jpg` | video | Candidate scene-change frames (`select=gt(scene,T)`) at original resolution; Python applies minimum and maximum spacing (default at least one per 30 s, at most one per 2 s), thins evenly by index above `max_keyframes` (0 = no cap) and deletes dropped frames before they are written. `<n>` is the index; the timestamp of each is recorded in `keyframes.json` (`[{"n": 0, "t": 12.4}, ...]`). |
| `audio.opus` | video with an audio stream, audio | Normalised track the ASR processor consumes (mono, 16 kHz, Opus). An `audio` input ignores any video stream; a video without audio stream gets none. |
| `web.mp4` / `web.m4a` / `web.png` | video / audio / image | Browser-playable rendition (see below). Not written when the original already plays (the consumer then serves the original; `renditions.json` says `original`). |
| `frames/<resolution>-<fps>.mp4` or `.mkv` | video | Frame store, one file per configured pass (below). |
| `decode.json` | all | What was done: stream facts used for decisions, keyframe timestamps, rendition choice per kind, frame passes with their codec, dropped-frame counts. |

### Renditions

- **Video:** the original if it is H.264/AAC in a browser-friendly container with faststart; a cheap remux (`-c copy -movflags +faststart`) if only container or
  faststart is wrong; otherwise a transcode downscaled to `web_max_height` (default 720), preset `veryfast`, crf about 28, 30 fps cap. Output `web.mp4`.
- **Audio:** the original if browsers play it (MP3, AAC/M4A, WAV, FLAC, Ogg Vorbis/Opus); otherwise AAC-LC in `.m4a` at 128 kbps. Output `web.m4a`.
- **Image:** JPEG/PNG/WebP/GIF as they are; HEIC, TIFF and other non-web formats to PNG at full resolution, no size cap (`pillow-heif` for HEIC). Output `web.png`.
- Option `web_rendition` = `auto` | `off`.

### Frame store

Storage is cheap, decoding is not: later per-frame analysis (OCR in [frame-ocr](frame-ocr.md), face detection in a consumer) reads these files instead of
decoding the original.

- Option `frame_passes`: list of `<resolution>@<fps>`; `resolution` is the output height in pixels or `full`, `fps` a number or `full`. Default
  `["540@10", "full@0.5"]`; empty list = none. Any number of entries.
- Codec chosen from the fps: H.264 for fps >= 2 (small, cheap to decode, read sequentially), all-intra MJPEG (`-q:v 3`, every frame a standalone JPEG, exact
  seeking) below 2. Fixed threshold.
- Sizes for a 2 h video with the defaults: the 540p/10 fps file about 100 MB; the `full@0.5` file about 1 GB at 1080p, 3.5 GB at 4K. Output limits for the
  `video` class must be raised by the operator to fit (`output_mib`).
- Images need no frame files (the image is its own frame).

### Options (`options["media-decode"]`)

`thumb_side`, `keyframe_scene_threshold`, `keyframe_min_gap_s`, `keyframe_max_gap_s`, `max_keyframes`, `web_rendition`, `web_max_height`, `frame_passes`.
Operator-side numbers (deadline, memory, output size) stay in `PARSE_LIMITS`.

## Open questions

1. **Handing artefacts to later processors in the same job** is solved in [processor-handoff](processor-handoff.md) (scratch is kept until the job ends, `ctx.dir()`); this plan only fixes the
   names `media-decode` writes under its `ctx.dir()` (`audio.opus`, `keyframes/`, `keyframes.json`, `frames/`, `decode.json`).
2. **Metadata completeness.** The `metadata` processor must provide, at least for the consumer's use: EXIF `DateTimeOriginal` with `OffsetTime`, GPS, image
   width/height, video/audio `creation_time`, an ISO 6709 location tag, duration, stream list. Verify during implementation; any missing field is added to
   `metadata`, not here.
3. **Wall-clock deadline.** One job runs decode (and, in the consumer's setup, ASR and captions) under the `video` class deadline (3600 s default). A 2 h transcode
   may not fit; document the setting to raise `deadline_s`. Splitting decode and model work into two jobs is the alternative if that turns out to be impractical.

## Implementation order

1. [processor-handoff](processor-handoff.md) implemented first.
2. `media-decode` processor: probing and decisions (pure functions, unit-tested on small synthetic files made with ffmpeg), the single ffmpeg command builder,
   keyframe spacing and thinning (pure function with tests), renditions, frame store.
3. Entry point, `files` extra, parser image (`ffmpeg`, `pillow-heif` are needed; ffmpeg is already installed).
4. Tests: a tiny video with a scene change, an audio file, a HEIC image; asserts on the outputs, the index entries and that the original is decoded once.
5. Docs.

## Files to change

| File | Change |
|---|---|
| `server/cadmus_parser_server/media_decode.py` (new) | the processor |
| `server/pyproject.toml` | entry point `media-decode`; `files` extra gains `pillow`, `pillow-heif` |
| `server/Dockerfile`, `server/install.sh` | dependencies |
| `server/cadmus_parser_server/tests/` | tests above |

## Docs to update

`docs/processors.md` (the new default processor, the handoff convention), `docs/reference.md` (options, `top_dirs`, outputs, limits), `README.md` (what it can parse), `docs/parser-image.md` (dependencies).
