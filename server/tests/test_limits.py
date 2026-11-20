"""Per-class limits: classification, resolution order, the per-limit maximum over mixed trees."""

import pytest

from cadmus_parser_server import limits


def write(tmp_path, name, data=b"x"):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_classify_extension_first_then_magic(tmp_path):
    assert limits.classify(write(tmp_path, "a.MP4")) == "video"
    assert limits.classify(write(tmp_path, "a.pdf", b"PK\x03\x04")) == "document"  # the extension wins
    for data, want in ((b"\x89PNG\r\n\x1a\n", "image"), (b"%PDF-1.7", "document"), (b"PK\x03\x04..", "archive"), (b"ID3\x03", "audio"),
                       (b"\0\0\0\x18ftypmp42", "video"), (b"RIFF\0\0\0\0WAVEfmt ", "audio"), (b"RIFF\0\0\0\0AVI LIST", "video"), (b"hello", "other"), (b"", "other")):
        assert limits.classify(write(tmp_path, "noext", data)) == want, data


def test_resolve_order_and_validation():
    t = limits.resolve({"default": {"deadline_s": 100}, "image": {"ram_mib": 44}})
    assert t["image"]["ram_mib"] == 44 and t["image"]["deadline_s"] == 100 and t["document"]["ram_mib"] == 6144
    assert t["video"]["deadline_s"] == 100 and t["video"]["input_mib"] == 20480  # default.* overrides the built-in, which stays for what it leaves
    assert all(v != 0 for c in t.values() for v in c.values())  # every built-in is finite, for every class
    t = limits.resolve({"video": {"deadline_s": 5}})
    assert t["video"]["deadline_s"] == 5 and t["audio"]["deadline_s"] == 1800
    for bad in ({"nope": {}}, {"default": {"nope": 1}}, {"default": {"ram_mib": -1}}, {"default": {"ram_mib": "x"}}, {"default": 5}):
        with pytest.raises(ValueError):
            limits.resolve(bad)


def test_merge_is_the_maximum_per_limit_and_zero_is_unlimited():
    t = limits.resolve({})
    m = limits.merge(t, {"image", "video"})
    assert m["deadline_s"] == 3600 and m["ram_mib"] == 16384 and m["nproc"] == 1024
    assert limits.merge(t, {"image"}) == t["image"] and limits.merge(t, set()) == t["other"]
    t = limits.resolve({"video": {"input_mib": 0}})
    assert limits.merge(t, {"image", "video"})["input_mib"] == 0 and limits.ceiling(t, "input_mib") == 0
    assert limits.ceiling(limits.resolve({}), "input_mib") == 20480
