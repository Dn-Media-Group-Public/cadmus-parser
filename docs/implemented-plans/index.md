# Implemented plans

- [processor-handoff](01-processor-handoff.md) — scratch kept until job end, `ctx.dir()`, `Member`, synthetic members, `recurse_many`
- [media-decode](02-media-decode.md) — `media-decode` processor: thumbnail, keyframes, `audio.opus`, browser-playable rendition, frame store
- [sidecar-usage-reporting](03-sidecar-usage-reporting.md) — job tokens + admin routes on sidecars; per-attempt `usage` in `status.json`
- [asr](04-asr.md) — `asr-sidecar` and the `asr-split`/`asr-primary`/`asr-fallback`/`asr-degrade` processors; chunked transcription with validation, fallback and speaker-mapped merge
