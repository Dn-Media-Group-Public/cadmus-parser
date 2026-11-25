# How to write a processor

A processor is the code that interprets a file. It runs inside the sandboxed parser process: **no storage access, no network, no credential**. It sees a file on
disk and writes artefacts through a sink; the wrapper moves the bytes.

## Registering

A Python package registers one or more entry points in the group `cadmus_parser.processors` (the group name is the same for every processor, whatever service
loads it):

```toml
[project.entry-points."cadmus_parser.processors"]
upper = "toy.processor:Upper"
```

Each resolves to a processor object (or a class, which is instantiated with no arguments) with:

| Member | Meaning |
|---|---|
| `name` | unique; the key of `options` in the job request |
| `before`, `after` | optional tuples of processor names: `before` = processors that must run after this one, `after` = must run before it. Names that are not installed are ignored; either side may declare an edge |
| `top_dirs` | the top-level directories (below the output base) this processor may write; the wrapper allows only the union over installed processors |
| `accepts(file) -> bool` | cheap; may look at `file.name`, `file.size`, the first bytes |
| `process(sink, file, ctx)` | do the work |

Nothing the caller sends is imported, and the client does not select processors: an image has none unless it installs one, and has whichever packages it installs. A
processor whose import fails (an extra that is not installed) is skipped with a line on the parser's stderr.

## The default processors

The `files` extra installs ten, one per kind of file or library: `metadata` (ExifTool, ffprobe, GDAL, OOXML, PDF history), `sizecap`, `archives`, `markdown`, `binary`
(msgpack, CBOR, BSON, plist, protobuf), `html` (trafilatura), `apk`, `documents` (markitdown and OCR, the fallback; an image gets `parsed/*.md` only from OCR, and none when OCR finds no text — [plan](implemented-plans/image-text-only-ocr.md)) `members` (keeps unpacked archive members) and `media-decode`. `media-decode` is separate: it has its own options entry and never claims a file (see below). The others
share the options entry `files` (`base_url`, `ctypes`, read with `ctx.options_of("files")`) and use `before`/`after` and `ctx.claim()` to hand a file to exactly one
content handler. They are the model for splitting your own.

## Order and dispatch

The worker sorts the processors topologically at the start of every job; free choices are broken alphabetically by name, so the order is the same on every run.
A **cycle fails the job at once** with the processors involved in `status.json`. For each file the worker runs **every processor whose `accepts` is true, in
order**; each adds its own outputs. A processor may claim a file (`ctx.claim()`), which stops the later ones for that file. Two writes of the same output path
in a job are refused: the later is dropped and noted on its input's entry.

A file no processor accepts gets the note "not parsed (no processor)", which is not a failure. A processor that raises leaves a note on that entry and the rest
continue. The parser is still one process per job: a crash or a limit kill ends the whole job.

## The interface

```python
class Upper:
    name = "upper"
    top_dirs = ("upper",)

    def accepts(self, file):
        return file.name.endswith(".txt")

    def process(self, sink, file, ctx):
        sink.info(content_type="text/plain")
        with open(file.path, "rb") as f:
            sink.put_bytes(f"upper/{file.rel}", f.read().upper(), "text/plain", meta={"shouted": True})
```

- `file.path` is the file on disk; `file.name` the last path element; `file.address` the input address as a list, one element per archive level
  (`["a.zip", "docs/m.pdf"]`); `file.rel` that address as one name for naming outputs (`a.zip.d/docs/m.pdf`); `file.depth` 0 for an input, more for an archive
  member; `file.size`.
- `sink.put_bytes(rel, data, content_type, meta=None)` and `sink.put_file(rel, src, content_type, meta=None)` (`src`, a file in `ctx.work`, also goes to the
  output, as a hard link; it stays in scratch) write `rel`, a path below the output base starting with one of your `top_dirs` and at least one more element. `sink.info(content_type=None, meta=None,
  size=None)` says something about the input itself (`size` counts only for archive members); `sink.note(text)` adds a note to its entry. `meta` is a small
  JSON object (at most 2000 bytes) passed through to `index.json`, e.g. `{"text": true}`. All of it is untrusted by the client: the parser may be hostile.
- `ctx.options` is this processor's own entry of the job's `options`; `ctx.features` the feature names the job asked for (their `<PREFIX>_URL` /
  `<PREFIX>_TOKEN` are in the environment, for the profile it named); `ctx.work` the job's scratch directory. Scratch is
  kept until the job ends (then wiped), so it is the handoff area: write files under `ctx.dir()` (`<work>/<processor>/<file.rel>/`, created) and later processors
  of the same job read them. Do not rewrite in place a file you gave to `put_file` (it shares the inode with the output): write a new one. The primary,
  fallback, last-resort pattern: a failing processor saves its salvaged attempt as `attempt.json` in its `ctx.dir()` and returns without `ctx.claim()`; the
  next one reads it from `<work>/<first>/<file.rel>/`.
- **Archives.** Unpack into `ctx.work` yourself and call `ctx.recurse(path, name)` for each member: the worker runs the same chain on it (as a `File` with a longer
  address, up to 8 levels) and builds the nested entries. No processor needs archive logic of its own. Respect your own bounds (bytes, members).
  `ctx.recurse` returns a `Member` (`.address`, `.claimed_by`, `.outputs`, `.dir(processor)`). `recurse(path, name, synthetic=True)` is for a chunk or window
  that is not a real archive member: it gets no `index.json` entry, its notes and outputs land on the nearest real ancestor's entry, and it counts against
  `MAX_SYNTHETIC_MEMBERS` (default 10000) instead of `MAX_ARCHIVE_MEMBERS`. `ctx.recurse_many([(path, name), …], workers=4)` runs the members in a thread
  pool, results in input order; `workers` is capped by `PARSE_MEMBER_WORKERS`, and a processor with `threadsafe = False` makes the pool serial.
  See also: [Plan: processor handoff](implemented-plans/01-processor-handoff.md)

## Testing

Drive the worker's `Runner` with an in-memory sink (see `server/tests/conftest.py`: `MemSink`), or run a whole job against `file://` URLs with the wrapper
(`server/tests/harness.py`). Settings the operator tunes reach the parser only through names listed in `PARSE_PASS_ENV`.

## media-decode

Decodes an image, video or audio file once (recognised by `filetype.detect`: libmagic on the bytes, the name only where the bytes are generic) and writes, below `media/<file.rel>/`: `thumb.jpg` (image, video), `keyframes/<n>.jpg` +
`keyframes.json` (video; scene changes, at least one per `keyframe_max_gap_s`, none closer than `keyframe_min_gap_s`), `audio.opus` (mono 16 kHz; video with audio, audio),
`web.mp4` / `web.m4a` / `web.png` (a browser-playable rendition, only when the original does not play; `renditions.json` says `original` otherwise), `frames/<resolution>-<fps>.mp4|.mkv`
(video frame store, one file per `frame_passes` entry) and `decode.json`. A video is decoded by a single ffmpeg invocation with several outputs. The same files are left under
`ctx.dir()` for processors of the same job (declare `after = ("media-decode",)`).

Options (`options["media-decode"]`): `thumb_side` (320), `keyframe_scene_threshold` (0.3), `keyframe_min_gap_s` (2), `keyframe_max_gap_s` (30), `max_keyframes` (0 = no cap),
`web_rendition` (`auto` | `off`), `web_max_height` (720), `frame_passes` (default `["540@10", "full@0.5"]`; `<height|full>@<fps|full>`; fps >= 2 is H.264, below that all-intra MJPEG; `[]` = none).
The frame store is large (a 2 h video: about 100 MB for `540@10`, 1 GB for `full@0.5` at 1080p): raise `output_mib` and, for long videos, `deadline_s` of the `video` class in `PARSE_LIMITS`.
Needs `ffmpeg` (with libx264 and libopus) and, for images, `Pillow` and `pillow-heif` (both in the `files` extra).
Size caps (operator env, read at import): `MAX_PARSE_BYTES_IMAGE`, `MAX_PARSE_BYTES_VIDEO`, `MAX_PARSE_BYTES_SOUND` (default 2 GiB each; `0` = no cap; the type is sniffed from the magic bytes). Over its cap a file is not decoded: it is stored and gets metadata only, and `sizecap` notes `<rel> not decoded (N bytes > MAX_PARSE_BYTES_VIDEO)`. The `files` processors are capped by `MAX_PARSE_BYTES_FILES` (default 50 MiB). Each variable that is unset or empty falls back to `MAX_PARSE_BYTES`, then the default. `asr`, `caption` and `frame-ocr` have none: they only act on `media-decode` output.
See also: [Plan: per-processor size caps](implemented-plans/parse-bytes-per-processor.md)

See also: [Plan: media decode](implemented-plans/02-media-decode.md)

## asr

Four processors (`asr-split`, `asr-primary`, `asr-fallback`, `asr-degrade`; see [reference](reference.md#asr)) and the model for any "primary, fallback, last resort" work on pieces of a file:
the parent (`asr-split`, after `media-decode`) cuts `audio.opus` into chunk **files** in its `ctx.dir()` (with a `chunks.json`) and calls
`ctx.recurse_many(..., synthetic=True)` on them; the chain runs on each chunk. `asr-primary` accepts only chunks made by `asr-split` (a synthetic member with a
`chunk-NNNN.opus` name and a `chunks.json` beside it), calls the sidecar, validates (`modelcheck.py`), keeps the result in its `ctx.dir()` (`segments.json`) and **claims** the chunk.
On failure it leaves `attempt.json` and does not claim; `asr-fallback` (`after = ("asr-primary",)`) then tries the other profile, and `asr-degrade` keeps the best salvaged attempt and always
claims. The parent reads each chunk's `segments.json` from `member.dir(member.claimed_by)` and writes the only output. `metadata` and `media-decode` skip synthetic members
(`file.index_address is not None`), and the ASR processors are ordered `before` the content handlers so `documents` never sees a chunk.
See also: [Plan: ASR](implemented-plans/04-asr.md)

## frame-ocr

`frame-ocr` (after `media-decode`, `before` the content handlers; see [reference](reference.md#frame-ocr)) runs only for a video when the job asked for the `ocr` feature. It picks one
frame pass from `decode.json`, has ffmpeg decode it sequentially to raw grey frames, skips frames that barely differ from the last OCRed one (pure `thumb_diff`), calls the engine
`ocr.ocr_engine` (shared with `documents`) and merges runs of near-identical text (pure `ocr_frames`). It claims nothing; its only output is `ocr.json`.
See also: [Plan: Frame OCR](implemented-plans/06-frame-ocr.md)

## caption

Four processors (`caption-split`, `caption-primary`, `caption-fallback`, `caption-skip`; see [reference](reference.md#caption)), the same shape as [asr](#asr): the parent
(`caption-split`, after `media-decode`) writes one **unit file** per captionable unit into its `ctx.dir()` (`window-NNNN.json`: kind, frame paths and times; an image is one unit
pointing at the original or its `web.png`) and calls `ctx.recurse_many(..., synthetic=True)`. `caption-primary` accepts only such synthetic files, scales the frames, calls the sidecar,
validates (`modelcheck.evaluate_text`) and keeps `caption.json` in its `ctx.dir()` and claims; on failure it leaves `attempt.json`. `caption-fallback` tries the other profile,
`caption-skip` claims what is left with an empty, `blocked` or `invalid` result (there is no degraded text worth keeping). The parent writes the only output, `captions.json`.
See also: [Plan: Caption](implemented-plans/05-caption.md)

## Content type detection

`filetype.detect(path, name)` is the one place that decides what a file is: libmagic (`python-magic`) on the whole file, refined by `mimetypes` on the file's name only where libmagic answers generically (`filetype.GENERIC`: octet-stream, plain ZIP/text, OLE, JSON/XML, empty). A name never overrides self-identifying bytes. `options.files.ctypes[rel]` still wins over both. `name` is the file's real name at any depth, so a consumer names its input (a prefix input holding `report.docx`) rather than passing a type. `limits.classify` and `media-decode` use the same detector.

See also: [Plan: file type detection](../implemented-plans/07-filetype-detection.md)
