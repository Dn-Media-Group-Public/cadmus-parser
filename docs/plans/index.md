# Plans

Unimplemented feature plans. Each is a draft until the maintainers agree it; "Open questions" lists what still needs a decision.
They were written for the article-graph consumer (its `docs/plans/cadmus-parsing.md`, which depends on all of them) but are written as general cadmus features.

| Plan | Adds | Depends on |
|---|---|---|
| [asr](04-asr.md) | `asr-sidecar` and processors: audio cut into chunk files, primary/fallback by processor priority, merge with speaker mapping, `transcript.json` | processor-handoff, media-decode, sidecar-usage-reporting |
| [caption](05-caption.md) | `caption-sidecar` and processors: image and keyframe-window captions, primary/fallback by processor priority | processor-handoff, media-decode, sidecar-usage-reporting |
| [frame-ocr](06-frame-ocr.md) | OCR of video frames from the frame store, through the existing OCR sidecar | processor-handoff, media-decode |

Implementation order: `processor-handoff` (done: [implemented](../implemented-plans/01-processor-handoff.md)) then `media-decode` (done: [implemented](../implemented-plans/02-media-decode.md)), and `sidecar-usage-reporting` (done: [implemented](../implemented-plans/03-sidecar-usage-reporting.md)), then `asr`, `caption`, `frame-ocr`.
