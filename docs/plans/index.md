# Plans

Unimplemented feature plans. Each is a draft until the maintainers agree it; "Open questions" lists what still needs a decision.
They were written for the article-graph consumer (its `docs/plans/cadmus-parsing.md`, which depends on all of them) but are written as general cadmus features.

| Plan | Adds | Depends on |
|---|---|---|
| [parse-bytes-per-processor](parse-bytes-per-processor.md) | `MAX_PARSE_BYTES` renamed `MAX_PARSE_BYTES_FILES`; new `MAX_PARSE_BYTES_MEDIA` for media-decode (supersedes the next row) | media-decode |
| [media-decode-max-bytes](media-decode-max-bytes.md) | `options["media-decode"]["max_bytes"]`: files over it are not decoded | media-decode |

Implementation order: `processor-handoff` (done: [implemented](../implemented-plans/01-processor-handoff.md)) then `media-decode` (done: [implemented](../implemented-plans/02-media-decode.md)), and `sidecar-usage-reporting` (done: [implemented](../implemented-plans/03-sidecar-usage-reporting.md)), and `asr` (done: [implemented](../implemented-plans/04-asr.md)), and `caption` (done: [implemented](../implemented-plans/05-caption.md)), and `frame-ocr` (done: [implemented](../implemented-plans/06-frame-ocr.md)).

The `oauth2-upstream-auth` plan is done: [implemented](../implemented-plans/oauth2-upstream-auth.md).
