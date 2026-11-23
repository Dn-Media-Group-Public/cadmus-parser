"""modelcheck: validation of one model answer on synthetic responses."""

import json

from cadmus_parser_server import modelcheck as mc


def seg(t, text="hello there everyone", spk="Person 1"):
    return {"start": t, "speaker": spk, "text": text}


def raw(*segs, tail=""):
    return json.dumps({"segments": list(segs)}) + tail


def test_ok_and_time_formats():
    r = mc.evaluate({"raw": raw(seg("00:05"), seg("1:10.5"), seg(130)), "finish_reason": "stop"}, 150)
    assert r.status == "ok" and [s["offset"] for s in r.segments] == [5, 70.5, 130]


def test_block_wins_over_everything():
    assert mc.evaluate({"raw": raw(seg(1)), "finish_reason": "content_filter"}, 60).status == "blocked"
    assert mc.evaluate({"raw": "", "finish_reason": "stop", "block_reason": "SAFETY"}, 60).status == "blocked"


def test_truncated_answer_keeps_its_complete_leading_segments_but_is_invalid():
    cut = raw(seg(1), seg(2))[:-30]  # the last element is cut off
    r = mc.evaluate({"raw": cut, "finish_reason": "length"}, 60)
    assert r.status == "invalid" and len(r.segments) == 1 and "truncated" in r.reason
    assert mc.evaluate({"raw": "garbage", "finish_reason": "stop"}, 60).status == "invalid"
    assert mc.decode_elements('[{"a":1},{"b":') == [{"a": 1}]


def test_bad_elements_are_skipped():
    r = mc.evaluate({"raw": json.dumps([{"text": 5, "start": 1}, {"text": "x", "start": "zz"}, seg(3)]), "finish_reason": "stop"}, 10)
    assert r.status == "ok" and len(r.segments) == 1


def test_quality_rules():
    assert mc.evaluate({"segments": [seg(10), seg(5)], "finish_reason": None}, 60).reason == "timestamps go backwards"
    assert "before the chunk end" in mc.evaluate({"segments": [seg(10)]}, 1000).reason  # 990 s > max(120, 200)
    ok = mc.evaluate({"segments": [seg(10)]}, 100)  # 90 s gap: inside the tolerance, noted
    assert ok.status == "ok" and ok.notes
    assert mc.evaluate({"segments": [seg(5, "word " * 11)]}, 10).status == "invalid"
    loop = " ".join(["we went to the store and bought milk"] * 4)
    assert "loop" in mc.evaluate({"segments": [seg(5, loop)]}, 10).reason
    assert mc.evaluate({"segments": [seg(5, "ha " * 10)]}, 10).status == "ok"  # 10 in a row is still fine
