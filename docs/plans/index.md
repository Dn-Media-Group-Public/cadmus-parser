# Plans

Unimplemented feature plans. Each is a draft until the maintainers agree it; "Open questions" lists what still needs a decision.
They were written for the article-graph consumer (its `docs/plans/cadmus-parsing.md`, which depends on all of them) but are written as general cadmus features.

| Plan | Adds | Depends on |
|---|---|---|
| [processor-handoff](processor-handoff.md) | worker support: handoff area between processors, `recurse` results, parallel members | none |
| [media-decode](media-decode.md) | thumbnail, keyframes, `audio.opus`, browser-playable rendition and frame store, in one ffmpeg pass | processor-handoff |
| [sidecar-usage-reporting](sidecar-usage-reporting.md) | per-attempt model usage recorded by the sidecar, written by the wrapper into `status.json` | none |
| [asr](asr.md) | `asr-sidecar` and processors: audio cut into chunk files, primary/fallback by processor priority, merge with speaker mapping, `transcript.json` | processor-handoff, media-decode, sidecar-usage-reporting |
| [caption](caption.md) | `caption-sidecar` and processors: image and keyframe-window captions, primary/fallback by processor priority | processor-handoff, media-decode, sidecar-usage-reporting |
| [frame-ocr](frame-ocr.md) | OCR of video frames from the frame store, through the existing OCR sidecar | processor-handoff, media-decode |

Implementation order: `processor-handoff` then `media-decode`, and `sidecar-usage-reporting` (independent of both), then `asr`, `caption`, `frame-ocr`.
