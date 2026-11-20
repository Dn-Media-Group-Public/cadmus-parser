"""media-decode: pure decisions (always run) and end-to-end runs on tiny synthetic files (need ffmpeg, Pillow, pillow-heif)."""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from cadmus_parser_server import media_decode as md
from cadmus_parser_server import processors, worker
from conftest import MemSink

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_select_keyframes_spacing_and_even_thinning():
    assert md.select_keyframes([0, 1, 3, 3.5, 10, 20], 2, 0) == [0, 2, 4, 5]
    assert md.select_keyframes(list(range(0, 100, 3)), 2, 4) == [0, 11, 22, 33]
    assert md.select_keyframes([0, 5, 9], 2, 1) == [0]


def test_frame_passes_codec_by_fps_and_rejects_junk():
    ok, bad = md.parse_passes(["540@10", "full@0.5", "full@full", "540@10", "x", "1080@0"])
    assert [(n, c) for _, _, n, c in ok] == [("540-10", "h264"), ("full-0.5", "mjpeg"), ("full-full", "h264")]
    assert bad == ["x", "1080@0"]
    assert md.parse_passes([]) == ([], [])


def test_sniff_by_magic_not_name():
    assert md.sniff(b"\x89PNG\r\n\x1a\n" + b"\0" * 8) == "image"
    assert md.sniff(b"\0\0\0\x18ftypheic\0\0\0\0") == "image"
    assert md.sniff(b"\0\0\0\x18ftypisom\0\0\0\0") == "video"
    assert md.sniff(b"ID3\x04" + b"\0" * 12) == "audio"
    assert md.sniff(b"just text......") is None


def test_one_input_for_the_whole_video_command():
    f = md.facts({"format": {"format_name": "matroska,webm", "duration": "60"}, "streams": [
        {"codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p", "avg_frame_rate": "60/1", "width": 1920, "height": 1080},
        {"codec_type": "audio", "codec_name": "opus"}]})
    assert md.video_rendition(f, "/nonexistent", b"") == "transcode"
    cmd = md.build_ffmpeg("in.mkv", "out", f, md.DEFAULTS, md.parse_passes(md.DEFAULTS["frame_passes"])[0], "transcode")
    assert cmd.count("-i") == 1
    for name in ("thumb.jpg", "keyframes/%d.jpg", "audio.opus", "web.mp4", "frames/540-10.mp4", "frames/full-0.5.mkv"):
        assert f"out/{name}" in cmd
    assert "fps=30" in " ".join(cmd)  # 60 fps source capped for the web rendition


def ff(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def decode(name, data, options=None):
    work, tree = tempfile.mkdtemp(), tempfile.mkdtemp()
    open(f"{tree}/{name}", "wb").write(data)
    s = MemSink()
    worker.Runner(s, processors.order([md.MediaDecode()]), {"media-decode": options or {}}, [], work).run_tree(tree)
    return s


@needs_ffmpeg
def test_video_with_a_scene_change_is_decoded_once(monkeypatch):
    d = tempfile.mkdtemp()
    ff("-f", "lavfi", "-i", "color=c=red:s=160x120:r=10:d=3", "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=10:d=3", "-f", "lavfi", "-i", "sine=d=6",
       "-filter_complex", "[0][1]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
       "-movflags", "+faststart", f"{d}/v.mp4")
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: (calls.append(cmd[0]), real(cmd, *a, **k))[1])
    s = decode("original", open(f"{d}/v.mp4", "rb").read(), {"keyframe_min_gap_s": 0.5, "frame_passes": ["60@10", "full@0.5"]})
    assert calls.count("ffmpeg") == 1  # a browser-playable original: no remux, no transcode
    base = "media/original"
    for n in ("thumb.jpg", "audio.opus", "keyframes.json", "decode.json", "renditions.json", f"keyframes/0.jpg", "frames/60-10.mp4", "frames/full-0.5.mkv"):
        assert f"{base}/{n}" in s.files, n
    assert f"{base}/web.mp4" not in s.files and json.loads(s.files[f"{base}/renditions.json"]) == {"web": "original"}
    kf = json.loads(s.files[f"{base}/keyframes.json"])
    assert any(2.5 < k["t"] < 3.5 for k in kf)  # the red -> blue cut at 3 s
    assert not s.notes


@needs_ffmpeg
def test_audio_is_normalised_and_converted_for_browsers():
    d = tempfile.mkdtemp()
    ff("-f", "lavfi", "-i", "sine=d=1", "-c:a", "aac", "-f", "adts", f"{d}/a")  # bare ADTS AAC: not browser-playable
    s = decode("original", open(f"{d}/a", "rb").read())
    assert "media/original/audio.opus" in s.files and "media/original/web.m4a" in s.files and "media/original/thumb.jpg" not in s.files
    assert json.loads(s.files["media/original/renditions.json"])["how"] == "convert"


@needs_ffmpeg
def test_png_is_its_own_rendition_and_gets_a_thumbnail():
    pytest.importorskip("PIL")
    d = tempfile.mkdtemp()
    ff("-f", "lavfi", "-i", "color=c=green:s=640x480", "-frames:v", "1", f"{d}/i.png")
    s = decode("original", open(f"{d}/i.png", "rb").read())
    assert "media/original/thumb.jpg" in s.files and "media/original/web.png" not in s.files
    assert json.loads(s.files["media/original/renditions.json"]) == {"web": "original"}


def test_heic_becomes_png():
    pytest.importorskip("pillow_heif")
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    d = tempfile.mkdtemp()
    Image.new("RGB", (64, 48), "red").save(f"{d}/i.heic")
    s = decode("original", open(f"{d}/i.heic", "rb").read())
    assert "media/original/web.png" in s.files and "media/original/thumb.jpg" in s.files
