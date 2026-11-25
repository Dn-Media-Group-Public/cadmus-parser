# Example output trees

What the default `files` processors write for each kind of input. Paths are relative to `output.url`; `<rel>` is the input's path (for an archive member, the
archive's path + `.d/` + the member's path: `a.zip.d/docs/m.pdf`). Which files appear depends on the file and the job: a line marked *(if ...)* is optional.
The per-file record of what came out is `index.json` ([reference](reference.md#statusjson-and-indexjson)); `status.json` sits beside it and is not shown.
Output paths and processors: [processors](processors.md), [reference](reference.md).

Every input also gets `metadata/<rel>.yaml` *(if anything was found: EXIF/GPS, Office tracked changes and comments, GDAL/vector CRS and bounds, ffprobe streams, PDF history)*.
A file over its size cap (`MAX_PARSE_BYTES_*`) gets only that.

## Documents: Word, Excel, PowerPoint, PDF, EPUB, CSV, JSON, XML, plain text, ...

markitdown; a scanned PDF page with no text layer goes through OCR (`ocr` feature).

```
report.pdf  ->
metadata/report.pdf.yaml
parsed/report.pdf.md          full Markdown
terse/report.pdf.md           headings, links, images only (not written when empty)
```

## Markdown (`.md`, `.markdown`)

Already Markdown: no `parsed/`, the input is marked as text in `index.json`.

```
notes.md  ->
metadata/notes.md.yaml        (if any)
terse/notes.md
```

## Web pages (HTML)

trafilatura main-content extraction; links absolute when `options.files.base_url` is set.

```
page.html  ->
metadata/page.html.yaml       (if any)
parsed/page.html.md           main content as Markdown
simplified/page.html.md       simplified main-content view (if one could be made)
terse/page.html.md
```

## Binary structured formats: msgpack, CBOR, BSON, plist, protobuf, gRPC-web

```
data.msgpack  ->
metadata/data.msgpack.yaml    (if any)
parsed/data.msgpack.json      pretty-printed JSON
```

## Archives: zip, tar, tgz, 7z, rar, gz, bz2, xz

The archive itself gets no `parsed/`. Every member is unpacked and run through the same chain, and also kept as `original/`. Members nest up to 8 levels
(`a.zip.d/b.tar.d/c.pdf`), within `MAX_ARCHIVE_MEMBERS` and `MAX_ARCHIVE_BYTES`.

```
a.zip  ->
metadata/a.zip.yaml
original/a.zip.d/docs/m.pdf
original/a.zip.d/index.html
metadata/a.zip.d/docs/m.pdf.yaml
parsed/a.zip.d/docs/m.pdf.md
terse/a.zip.d/docs/m.pdf.md
parsed/a.zip.d/index.html.md
simplified/a.zip.d/index.html.md
terse/a.zip.d/index.html.md
```

## Android packages (`.apk`)

Stored for installing; nothing is parsed.

```
app.apk  ->
metadata/app.apk.yaml         (if any)
```

## Images

`media-decode` (thumbnail, rendition) always; text only from OCR (`ocr` feature), and a caption from a vision model (`caption` feature). No `parsed/` when
there is neither text nor caption. Video-only outputs (`frame-ocr/`, `keyframes/`, `frames/`) do not exist for an image.

```
photo.heic  ->
metadata/photo.heic.yaml      EXIF, GPS
media/photo.heic/thumb.jpg
media/photo.heic/web.png      only when the original does not play in a browser (renditions.json says "original" otherwise)
media/photo.heic/decode.json
media/photo.heic/renditions.json
caption/photo.heic/captions.json    (if the caption feature)
parsed/photo.heic.md          caption, then OCR text (if either exists)
```

## Video

```
clip.mp4  ->
metadata/clip.mp4.yaml        ffprobe streams, chapters
media/clip.mp4/thumb.jpg
media/clip.mp4/keyframes.json
media/clip.mp4/keyframes/0.jpg  1.jpg  ...      scene changes
media/clip.mp4/frames/540-10.mp4                frame store, one file per frame_passes entry
media/clip.mp4/frames/full-0.5.mkv
media/clip.mp4/audio.opus     mono 16 kHz (if the video has sound)
media/clip.mp4/web.mp4        only when the original does not play in a browser
media/clip.mp4/decode.json
media/clip.mp4/renditions.json
asr/clip.mp4/transcript.json        (if the asr feature)
caption/clip.mp4/captions.json      (if the caption feature)
frame-ocr/clip.mp4/ocr.json         (if the ocr feature)
parsed/clip.mp4.md            ## h:mm:ss windows: **Scene:**, **On screen:**, **Speech:**
```

## Audio

```
talk.mp3  ->
metadata/talk.mp3.yaml
media/talk.mp3/audio.opus
media/talk.mp3/web.m4a        only when the original does not play in a browser
media/talk.mp3/decode.json
media/talk.mp3/renditions.json
asr/talk.mp3/transcript.json        (if the asr feature)
parsed/talk.mp3.md            ## h:mm:ss windows with **Speech:** (if a transcript exists)
```

## Geodata: GeoTIFF, GPX, KML, GeoJSON, GeoPackage, shapefile

Only metadata (CRS, bounds) is extracted; the content goes through the generic document path.

```
track.gpx  ->
metadata/track.gpx.yaml
parsed/track.gpx.md           (as any text/XML file, if markitdown yields text)
terse/track.gpx.md
```

## A file nothing accepts

Stored, `index.json` notes "not parsed (no processor)"; this is not a failure.

## Your own format

Whatever your processor writes below its declared `top_dirs`, alongside the above ([processors](processors.md)).
