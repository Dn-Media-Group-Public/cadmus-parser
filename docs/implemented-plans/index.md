# Implemented plans

- [processor-handoff](01-processor-handoff.md) — scratch kept until job end, `ctx.dir()`, `Member`, synthetic members, `recurse_many`
- [media-decode](02-media-decode.md) — `media-decode` processor: thumbnail, keyframes, `audio.opus`, browser-playable rendition, frame store
- [sidecar-usage-reporting](03-sidecar-usage-reporting.md) — job tokens + admin routes on sidecars; per-attempt `usage` in `status.json`
- [asr](04-asr.md) — `asr-sidecar` and the `asr-split`/`asr-primary`/`asr-fallback`/`asr-degrade` processors; chunked transcription with validation, fallback and speaker-mapped merge
- [caption](05-caption.md) — `caption-sidecar` and the `caption-split`/`caption-primary`/`caption-fallback`/`caption-skip` processors; image and keyframe-window captions with validation and fallback
- [frame-ocr](06-frame-ocr.md) — `frame-ocr` processor: OCR of a video's frame-store frames, deduplicated over time, `ocr.json`
- [oauth2-upstream-auth](oauth2-upstream-auth.md) — `auth: oauth2` (client credentials, cached and refreshed) in the ocr/asr/caption sidecars, shared `common/upstream_auth.py`
- [filetype-detection](07-filetype-detection.md) — `filetype.detect`: libmagic on the bytes, the name only where the bytes are generic
- [parse-bytes-per-processor](parse-bytes-per-processor.md) — `MAX_PARSE_BYTES_{FILES,IMAGE,VIDEO,SOUND}`, `MAX_PARSE_BYTES` as fallback; media over its cap is not decoded
- [image-text-only-ocr](image-text-only-ocr.md) — images get `parsed/*.md` only when OCR finds text (no EXIF fallback)
- [media-markdown](media-markdown.md) — `media-markdown` processor: one time-interleaved `parsed/*.md` for images, video and audio
- [figure-extraction](figure-extraction.md) — `layout` feature (chains of profiles), `layout-sidecar` (yolo-onnx, docling-serve, openai-vlm), `page-ocr` and page units; figures of scanned pages as image members, `figures.json`, full-page Docling profiles, per-job OCR languages
