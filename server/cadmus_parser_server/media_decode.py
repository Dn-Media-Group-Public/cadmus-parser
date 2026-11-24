"""`media-decode`: decodes an image, video or audio file once and writes every derived artefact below `media/<file.rel>/` (thumbnail, scene-change
keyframes, normalised `audio.opus`, browser-playable rendition, frame store, `decode.json`). Video: one ffmpeg invocation with several outputs. Options:
`options["media-decode"]`, see DEFAULTS. Everything is also left under `ctx.dir()` for later processors of the job."""

import json
import os
import re
import shutil
import subprocess

from cadmus_parser_server.filetype import detect

DEFAULTS = {"thumb_side": 320, "keyframe_scene_threshold": 0.3, "keyframe_min_gap_s": 2.0, "keyframe_max_gap_s": 30.0, "max_keyframes": 0,
            "web_rendition": "auto", "web_max_height": 720, "frame_passes": ["540@10", "full@0.5"]}
INTRA_BELOW_FPS = 2  # frame passes below this many fps are all-intra MJPEG (exact seeking), at or above H.264
WEB_IMAGE = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
WEB_AUDIO = {"mp3", "flac", "wav", "pcm_s16le", "pcm_s24le", "pcm_u8", "vorbis", "opus"}  # ffprobe codec names; AAC is checked with the container
IMAGE_BRANDS = {b"heic", b"heix", b"mif1", b"msf1", b"avif", b"heim", b"heis"}
_PASS = re.compile(r"^(full|\d+)@(full|\d+(?:\.\d+)?)$")


# ---- pure helpers ---------------------------------------------------------------------------------------------------

def sniff(path: str, name: str | None = None) -> str | None:
    """'image' | 'video' | 'audio' | None by `detect`. Video may turn out audio-only at probe."""
    major = detect(path, name).split("/")[0]
    return major if major in ("image", "video", "audio") else None


def parse_passes(specs) -> tuple:
    """(['540@10', ...] valid, [bad ones]) -> ([(height|None, fps|None, name, codec)], [rejected]). None = `full`; name is the file stem."""
    out, bad, seen = [], [], set()
    for s in specs or []:
        m = _PASS.match(str(s))
        if not m or (m[1] != "full" and int(m[1]) < 2) or (m[2] != "full" and float(m[2]) <= 0):
            bad.append(str(s))
            continue
        h = None if m[1] == "full" else int(m[1])
        fps = None if m[2] == "full" else float(m[2])
        name = f"{m[1]}-{m[2] if fps is None else format(fps, 'g')}"
        if name in seen:
            continue
        seen.add(name)
        # full fps means every frame: as heavy as the original, so H.264 (like any high fps)
        out.append((h, fps, name, "mjpeg" if fps is not None and fps < INTRA_BELOW_FPS else "h264"))
    return out, bad


def select_keyframes(times: list, min_gap: float, max_keyframes: int) -> list:
    """Indices to keep of the candidate timestamps: none closer than `min_gap` to the previous kept one, then thinned evenly by index to `max_keyframes` (0 = all).
    The maximum gap is enforced upstream, by the select expression that makes ffmpeg emit a candidate when none was for that long."""
    kept, last = [], None
    for i, t in enumerate(times):
        if last is None or t - last >= min_gap:
            kept.append(i)
            last = t
    k = max_keyframes
    if k and len(kept) > k:
        kept = [kept[0]] if k == 1 else [kept[round(j * (len(kept) - 1) / (k - 1))] for j in range(k)]
    return kept


def faststart(path: str) -> bool:
    """MP4/MOV: is the `moov` box before `mdat`?"""
    with open(path, "rb") as f:
        pos = 0
        while (h := f.read(8)) and len(h) == 8:
            size, kind = int.from_bytes(h[:4], "big"), h[4:8]
            if kind == b"moov":
                return True
            if kind == b"mdat":
                return False
            if size == 1:
                size = int.from_bytes(f.read(8), "big")
            elif size == 0:
                return False
            if size < 8:
                return False
            pos += size
            f.seek(pos)
    return False


def video_rendition(f: dict, path: str, ftyp: bytes) -> str:
    """'original' | 'remux' | 'transcode' for a video, from probe facts `f` (see `facts`)."""
    codecs_ok = f["vcodec"] == "h264" and f["pix_fmt"] == "yuv420p" and f["acodec"] in (None, "aac")
    if not codecs_ok:
        return "transcode"
    if "mp4" in f["format"] and ftyp not in (b"qt  ",) and faststart(path):
        return "original"
    return "remux"


def audio_rendition(f: dict) -> str:
    """'original' | 'convert' for an audio file."""
    fmt, c = f["format"], f["acodec"]
    ok = c in WEB_AUDIO or (c == "aac" and "mp4" in fmt)  # bare ADTS AAC (format `aac`) does not play in browsers
    return "original" if ok else "convert"


def facts(probe: dict) -> dict:
    """ffprobe JSON -> the stream facts decisions are made from."""
    ss = probe.get("streams") or []
    v = next((s for s in ss if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in ss if s.get("codec_type") == "audio"), None)
    num, _, den = ((v or {}).get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    try:
        dur = float((probe.get("format") or {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    return {"format": (probe.get("format") or {}).get("format_name", ""), "duration": dur,
            "vcodec": v and v.get("codec_name"), "pix_fmt": v and v.get("pix_fmt"), "width": v and v.get("width"), "height": v and v.get("height"), "fps": fps,
            "acodec": a and a.get("codec_name")}


def _scale(height) -> str:
    return "scale=trunc(iw/2)*2:trunc(ih/2)*2" if height is None else f"scale=-2:'min({height},ih)'"  # even sizes for H.264


def build_ffmpeg(src: str, out: str, f: dict, o: dict, passes: list, rendition: str) -> list:
    """The single ffmpeg command for a video: every output hangs off one input, so the source is decoded once."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info", "-y", "-protocol_whitelist", "file", "-i", src]
    side = int(o["thumb_side"])
    cmd += ["-map", "0:v:0", "-vf", f"select='gte(t,{f['duration'] * 0.1:.3f})',scale=w={side}:h={side}:force_original_aspect_ratio=decrease", "-frames:v", "1", "-fps_mode", "vfr",
            "-q:v", "4", "-update", "1", f"{out}/thumb.jpg"]
    sel = (f"gt(scene,{float(o['keyframe_scene_threshold'])})+isnan(prev_selected_t)"
           f"+gte(t-prev_selected_t,{float(o['keyframe_max_gap_s'])})")
    cmd += ["-map", "0:v:0", "-vf", f"select='{sel}',showinfo", "-fps_mode", "vfr", "-q:v", "3", "-start_number", "0", f"{out}/keyframes/%d.jpg"]
    if f["acodec"]:
        cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k", f"{out}/audio.opus"]
    if rendition == "transcode":
        vf = f"scale=-2:'min({int(o['web_max_height'])},ih)'" + (",fps=30" if f["fps"] > 30 else "")
        cmd += ["-map", "0:v:0", *(["-map", "0:a:0"] if f["acodec"] else []), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", f"{out}/web.mp4"]
    for height, fps, name, codec in passes:
        vf = ",".join(([f"fps={fps:g}"] if fps else []) + [_scale(height)])
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"] if codec == "h264" else ["-c:v", "mjpeg", "-q:v", "3", "-pix_fmt", "yuvj420p"]
        cmd += ["-map", "0:v:0", "-vf", vf, *enc, "-an", f"{out}/frames/{name}.{'mp4' if codec == 'h264' else 'mkv'}"]
    return cmd


def _options(raw: dict, notes: list) -> dict:
    o = {**DEFAULTS, **(raw or {})}
    try:
        for k, cast in (("thumb_side", int), ("keyframe_scene_threshold", float), ("keyframe_min_gap_s", float), ("keyframe_max_gap_s", float),
                        ("max_keyframes", int), ("web_max_height", int)):
            o[k] = cast(o[k])
        assert o["thumb_side"] > 0 and o["web_max_height"] > 0 and o["max_keyframes"] >= 0 and o["keyframe_max_gap_s"] > 0
        assert o["web_rendition"] in ("auto", "off") and isinstance(o["frame_passes"], list)
    except (ValueError, TypeError, AssertionError):
        notes.append("media-decode: bad options, defaults used")
        o = dict(DEFAULTS)
    return o


# ---- processing -----------------------------------------------------------------------------------------------------

def _run(cmd: list, stderr_path: str) -> int:
    with open(stderr_path, "wb") as err:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, check=False).returncode


def _probe(path: str) -> dict:
    p = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", "-protocol_whitelist", "file", path],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
    return json.loads(p.stdout)


def _keyframes(out: str, stderr_path: str, o: dict) -> tuple:
    """Candidate timestamps from showinfo, spacing and thinning; renumbers the kept files 0..k-1. Returns ([{n, t}], dropped)."""
    with open(stderr_path, errors="replace") as fh:
        times = [float(m[1]) for line in fh if "Parsed_showinfo" in line and (m := re.search(r"pts_time:\s*(-?[\d.]+)", line))]
    files = sorted(int(n[:-4]) for n in os.listdir(f"{out}/keyframes") if n.endswith(".jpg"))
    n = min(len(times), len(files))  # the two lists are the same frames in the same order; trust the shorter on a truncated run
    keep = select_keyframes(times[:n], float(o["keyframe_min_gap_s"]), int(o["max_keyframes"]))
    for i in files:
        if i not in keep:
            os.unlink(f"{out}/keyframes/{i}.jpg")
    for new, old in enumerate(keep):  # ascending, new <= old, dropped already gone: never overwrites a pending file
        if new != old:
            os.rename(f"{out}/keyframes/{old}.jpg", f"{out}/keyframes/{new}.jpg")
    return [{"n": new, "t": times[old]} for new, old in enumerate(keep)], len(files) - len(keep)


def _image(path: str, out: str, o: dict, d: dict, notes: list) -> dict:
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        notes.append("media-decode: pillow-heif not installed, HEIC unsupported")
    produced = {}
    with Image.open(path) as im:
        fmt = im.format
        im = ImageOps.exif_transpose(im)
        d.update(width=im.width, height=im.height, format=fmt)
        th = im.copy()
        th.thumbnail((int(o["thumb_side"]),) * 2)
        th.convert("RGB").save(f"{out}/thumb.jpg", "JPEG", quality=85)
        produced["thumb.jpg"] = "image/jpeg"
        if o["web_rendition"] == "auto":
            if fmt in WEB_IMAGE:
                d["renditions"] = {"web": "original"}
            else:
                im.convert("RGBA" if "A" in im.getbands() else "RGB").save(f"{out}/web.png", "PNG")  # full resolution, no size cap
                produced["web.png"] = "image/png"
                d["renditions"] = {"web": "web.png"}
    return produced


class MediaDecode:
    """Image, video or audio (by magic bytes): thumbnail, keyframes, `audio.opus`, browser-playable rendition, frame store. Runs after `metadata`."""
    name, top_dirs, after = "media-decode", ("media",), ("metadata",)
    before = ("sizecap", "archives", "markdown", "binary", "html", "apk", "documents")  # sizecap would claim a large video first

    def accepts(self, file) -> bool:
        if file.index_address is not None:  # a synthetic member (an ASR chunk) is already audio.opus
            return False
        return sniff(file.path, file.name) is not None

    def process(self, sink, file, ctx) -> None:
        notes: list = []
        o = _options(ctx.options, notes)
        base = ctx.dir()
        with open(file.path, "rb") as f:
            head = f.read(16)
        kind = sniff(file.path, file.name)
        d: dict = {"kind": kind}
        produced: dict = {}  # path below base -> content type
        try:
            if kind == "image":
                produced = _image(file.path, base, o, d, notes)
            else:
                produced = self._av(file, base, o, d, head, notes)
        except Exception as e:  # noqa: BLE001  one file never fails the job; what was written so far is kept
            notes.append(f"{file.rel}: media-decode failed ({type(e).__name__})")
        for n in notes:
            sink.note(n)
        with open(f"{base}/decode.json", "w") as fh:
            json.dump(d, fh)
        produced["decode.json"] = "application/json"
        if "renditions" in d:
            with open(f"{base}/renditions.json", "w") as fh:
                json.dump(d["renditions"], fh)
            produced["renditions.json"] = "application/json"
        for rel, ctype in produced.items():
            sink.put_file(f"media/{file.rel}/{rel}", f"{base}/{rel}", ctype)

    def _av(self, file, base, o, d, head, notes) -> dict:
        f = facts(_probe(file.path))
        kind = "video" if f["vcodec"] else "audio" if f["acodec"] else None
        d.update(kind=kind, **f)
        if kind is None:
            notes.append(f"{file.rel}: no audio or video stream")
            return {}
        passes, bad = parse_passes(o["frame_passes"])
        if bad:
            notes.append(f"media-decode: ignored frame_passes {bad}")
        auto = o["web_rendition"] == "auto"
        produced: dict = {}
        if kind == "video":
            for sub in ("keyframes", "frames"):
                os.makedirs(f"{base}/{sub}", exist_ok=True)
            how = video_rendition(f, file.path, head[8:12]) if auto else None
            cmd = build_ffmpeg(file.path, base, f, o, passes, how)
            err = f"{base}/ffmpeg.log"
            rc = _run(cmd, err)
            d["ffmpeg_exit"] = rc
            if rc:
                notes.append(f"{file.rel}: ffmpeg exit {rc}")
            if os.path.exists(f"{base}/thumb.jpg"):
                produced["thumb.jpg"] = "image/jpeg"
            if f["acodec"] and os.path.exists(f"{base}/audio.opus"):
                produced["audio.opus"] = "audio/ogg"
            kf, dropped = _keyframes(base, err, o)
            with open(f"{base}/keyframes.json", "w") as fh:
                json.dump(kf, fh)
            produced["keyframes.json"] = "application/json"
            produced.update({f"keyframes/{k['n']}.jpg": "image/jpeg" for k in kf})
            d.update(keyframes=len(kf), keyframes_dropped=dropped)
            d["frame_passes"] = []
            for _, _, name, codec in passes:
                ext = "mp4" if codec == "h264" else "mkv"
                if os.path.exists(p := f"{base}/frames/{name}.{ext}"):
                    produced[f"frames/{name}.{ext}"] = "video/mp4" if ext == "mp4" else "video/x-matroska"
                    d["frame_passes"].append({"file": f"frames/{name}.{ext}", "codec": codec})
            if how == "remux":
                rc = _run(["ffmpeg", "-nostdin", "-y", "-protocol_whitelist", "file", "-i", file.path, "-map", "0:v:0", *(["-map", "0:a:0"] if f["acodec"] else []),
                           "-c", "copy", "-movflags", "+faststart", f"{base}/web.mp4"], f"{base}/remux.log")
                how = "remux" if rc == 0 else "failed"
            if how in ("remux", "transcode") and os.path.exists(f"{base}/web.mp4"):
                produced["web.mp4"] = "video/mp4"
                d["renditions"] = {"web": "web.mp4", "how": how}
            elif how == "original":
                d["renditions"] = {"web": "original"}
        else:  # audio: any video stream (cover art) is ignored
            rc = _run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-protocol_whitelist", "file", "-i", file.path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                       "-c:a", "libopus", "-b:a", "32k", f"{base}/audio.opus"], f"{base}/ffmpeg.log")
            if rc:
                notes.append(f"{file.rel}: ffmpeg exit {rc}")
            elif os.path.exists(f"{base}/audio.opus"):
                produced["audio.opus"] = "audio/ogg"
            if auto:
                if audio_rendition(f) == "original":
                    d["renditions"] = {"web": "original"}
                elif not _run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-protocol_whitelist", "file", "-i", file.path, "-map", "0:a:0", "-vn", "-c:a", "aac",
                               "-b:a", "128k", f"{base}/web.m4a"], f"{base}/web.log"):
                    produced["web.m4a"] = "audio/mp4"
                    d["renditions"] = {"web": "web.m4a", "how": "convert"}
        return produced
