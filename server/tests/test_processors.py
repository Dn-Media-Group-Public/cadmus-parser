"""Processor registry and dispatch: ordering, cycles, claims, unhandled and failing files, archive recursion."""

import os
import tempfile

import pytest

from cadmus_parser_server import processors, worker
from conftest import MemSink


class P:
    def __init__(self, name, before=(), after=(), accept=True, fail=False, claim=False):
        self.name, self.before, self.after, self.top_dirs = name, before, after, ("out",)
        self.accept, self.fail, self.claim = accept, fail, claim

    def accepts(self, file):
        return self.accept(file) if callable(self.accept) else self.accept

    def process(self, sink, file, ctx):
        if self.fail:
            raise RuntimeError("boom")
        sink.put_bytes(f"out/{self.name}/{file.rel}", b"x", "a/b")
        if self.claim:
            ctx.claim()


def names(ps):
    return [p.name for p in processors.order(ps)]


def test_order_is_topological_alphabetical_and_ignores_missing_names():
    assert names([P("c"), P("a"), P("b")]) == ["a", "b", "c"]
    assert names([P("a", after=["c"]), P("b"), P("c")]) == ["b", "c", "a"]
    assert names([P("a"), P("b", before=["a", "nope"]), P("c", after=["nope"])]) == ["b", "a", "c"]  # either side may declare an edge


def test_a_cycle_names_the_processors():
    with pytest.raises(processors.CycleError, match="a, b"):
        processors.order([P("a", after=["b"]), P("b", after=["a"]), P("c")])
    with pytest.raises(processors.CycleError, match="named"):
        processors.order([P("a"), P("a")])


def run(procs, files, options=None):
    work, tree = tempfile.mkdtemp(), tempfile.mkdtemp()
    for n, data in files.items():
        os.makedirs(os.path.dirname(f"{tree}/{n}"), exist_ok=True)
        open(f"{tree}/{n}", "wb").write(data)
    s = MemSink()
    worker.Runner(s, processors.order(procs), options, [], work).run_tree(tree)
    return s


def test_every_accepting_processor_runs_in_order_unless_one_claims():
    s = run([P("a"), P("b", accept=lambda f: f.name.endswith(".md")), P("c")], {"x.md": b"1", "y.bin": b"2"})
    assert sorted(s.files) == ["out/a/x.md", "out/a/y.bin", "out/b/x.md", "out/c/x.md", "out/c/y.bin"]
    s = run([P("a", claim=True), P("b")], {"x": b"1"})
    assert sorted(s.files) == ["out/a/x"]


def test_unhandled_and_failing_files_are_notes_not_failures():
    assert run([P("a", accept=False)], {"x": b"1"}).notes == ["not parsed (no processor)"]
    s = run([P("a", fail=True), P("b")], {"x": b"1"})
    assert "out/b/x" in s.files and s.notes == ["a: x not parsed (RuntimeError: boom)"]


def test_recursion_addresses_and_depth_cap():
    class Arch(P):
        def process(self, sink, file, ctx):
            super().process(sink, file, ctx)
            p = os.path.join(ctx.work, f"m{file.depth}")
            open(p, "wb").write(b"m")
            try:
                ctx.recurse(p, "in.d")
            except ValueError as e:
                sink.note(str(e))
    s = run([Arch("arch")], {"a.zip": b"z"})
    assert len(s.files) == 8 and "out/arch/a.zip.d/in.d.d/in.d" in s.files  # depth 0..7; the member address is joined with ".d/"
    assert [n for n in s.notes if "nested more than 8" in n]


def test_scratch_is_kept_and_members_return_results():
    seen = {}

    class A(P):
        def process(self, sink, file, ctx):
            if file.depth:
                return
            p = os.path.join(ctx.dir(), "f.bin")
            open(p, "wb").write(b"kept")
            sink.put_file(f"out/a/{file.rel}", p, "a/b")
            os.makedirs(os.path.join(ctx.work, "c"), exist_ok=True)
            cp = os.path.join(ctx.work, "c", "x")
            open(cp, "wb").write(b"c")
            seen["m"] = ctx.recurse_many([(cp, "chunk-1"), (cp, "chunk-2")], synthetic=True)
            seen["f"] = p

    class B(P):
        def process(self, sink, file, ctx):
            sink.note("n:" + file.name)
            sink.put_bytes(f"out/b/{file.rel}", b"y", "a/b")
            ctx.claim()

    s = run([A("a"), B("b", accept=lambda f: f.depth > 0)], {"x.bin": b"1"})
    assert open(seen["f"], "rb").read() == b"kept" and s.files["out/a/x.bin"] == b"kept"  # source survives put_file
    m = seen["m"]
    assert [x.address[-1] for x in m] == ["chunk-1", "chunk-2"] and m[0].claimed_by == "b" and m[0].outputs == ["out/b/x.bin.d/chunk-1"]
    assert s.notes == ["n:chunk-1", "n:chunk-2"]


def test_synthetic_cap(monkeypatch):
    monkeypatch.setattr(worker, "MAX_SYNTHETIC_MEMBERS", 1)

    class A(P):
        def process(self, sink, file, ctx):
            if not file.depth:
                p = os.path.join(ctx.work, "m")
                open(p, "wb").write(b"m")
                ctx.recurse(p, "c1", synthetic=True)
                with pytest.raises(ValueError, match="synthetic"):
                    ctx.recurse(p, "c2", synthetic=True)

    run([A("a", accept=lambda f: not f.depth), P("b", accept=lambda f: f.depth > 0)], {"x": b"1"})
