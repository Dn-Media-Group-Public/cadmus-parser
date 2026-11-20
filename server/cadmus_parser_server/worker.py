"""The parser process (run by the wrapper as a pool uid): `python -I -u -m cadmus_parser_server.worker <jobdir>`.
<jobdir>/in/tree/…  is the input tree and in/job.json = {options: {<processor>: {…}}, features: […]}; artefacts are written under <jobdir>/out/
and announced to the wrapper, one JSON record per line, on a private dup of the stdout pipe. Every tool started from here writes to stderr.

Every installed processor is loaded and sorted (processors.order); each file of the tree goes through every processor whose `accepts` is true, in
order, unless one of them claims it. Archive members are handed back with `ctx.recurse`. The records name their input as a list of path
elements, one per archive level: ["a.zip", "docs/m.pdf"]."""

import os

PROTO = os.dup(1)  # before any import: nothing imported or spawned later can write to the protocol
os.dup2(2, 1)
os.set_inheritable(PROTO, False)

import json  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402

from cadmus_parser_server import processors  # noqa: E402

MAX_DEPTH = 8
LINE_MAX = 4096  # PIPE_BUF: one write per record is atomic


class Sink:
    """Where one input file's artefacts go. `rel` is a path relative to the output base, below a top_dir some processor declared."""

    def put_bytes(self, rel: str, data: bytes, ctype: str, meta: dict | None = None) -> None: ...
    def put_file(self, rel: str, src: str, ctype: str, meta: dict | None = None) -> None: ...  # moves `src` (a file in the work dir) into the output
    def info(self, content_type: str | None = None, meta: dict | None = None, size: int | None = None) -> None: ...  # about the input itself
    def note(self, text: str) -> None: ...


class PipeSinks:
    """One per job: the sinks of its input files, writing to the protocol pipe. An output path is announced once per job: a second write
    could be deleted by the wrapper after its upload, so it is refused and noted on the input that asked."""

    def __init__(self, out: str):
        self.out, self.sent = out, set()

    def announce(self, rec: dict) -> None:
        line = json.dumps(rec, separators=(",", ":")).encode() + b"\n"
        if len(line) > LINE_MAX and "meta" in (rec.get("info") or rec):
            rec = {k: v for k, v in rec.items() if k != "meta"}
            rec.get("info", {}).pop("meta", None)
            line = json.dumps(rec, separators=(",", ":")).encode() + b"\n"
        assert len(line) <= LINE_MAX, "record too long"
        os.write(PROTO, line)

    def fatal(self, text: str) -> None:
        self.announce({"error": text[:1000]})

    def for_file(self, address: list) -> Sink:
        return _PipeSink(self, address)


class _PipeSink(Sink):
    def __init__(self, sinks: PipeSinks, address: list):
        self.s, self.address = sinks, address

    def _dest(self, rel: str) -> str:
        dest = os.path.join(self.s.out, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        return dest

    def _fresh(self, rel: str) -> bool:
        if rel in self.s.sent:
            self.note(f"output {rel} written twice: the later write is refused")
            return False
        self.s.sent.add(rel)
        return True

    def _put(self, rel, ctype, meta):
        self.s.announce({"in": self.address, "path": rel, "content_type": ctype, **({"meta": meta} if meta else {})})

    def put_bytes(self, rel, data, ctype, meta=None):
        if self._fresh(rel):
            with open(self._dest(rel), "wb") as f:
                f.write(data)
            self._put(rel, ctype, meta)

    def put_file(self, rel, src, ctype, meta=None):
        if not self._fresh(rel):
            return os.unlink(src)
        shutil.copyfile(src, self._dest(rel))  # a copy, not a rename: the new file takes out/'s group, which is how the wrapper reads it
        os.unlink(src)
        self._put(rel, ctype, meta)

    def info(self, content_type=None, meta=None, size=None):
        info = {k: v for k, v in (("content_type", content_type), ("meta", meta), ("size", size)) if v is not None}
        self.s.announce({"in": self.address, "info": info})

    def note(self, text):
        self.s.announce({"in": self.address, "note": text[:1000]})


class File:
    """What a processor is asked about: `path` on disk, `address` (["a.zip", "docs/m.pdf"]), `rel` (the address as one name, "a.zip.d/docs/m.pdf",
    for naming outputs), `depth` (0 = an input file, > 0 = an archive member), `derived` (an input that an earlier run unpacked from an archive: it
    is parsed like any file, never unpacked itself)."""

    def __init__(self, path: str, address: list, derived: bool = False):
        self.path, self.address, self.derived = path, address, derived
        self.name, self.depth, self.rel = address[-1], len(address) - 1, ".d/".join(address)
        self.claimed = False
        self.size = os.path.getsize(path)


class Ctx:
    """What a processor gets besides the sink and the file. `claim()` stops the later processors for this file."""

    def __init__(self, runner: "Runner", file: File, proc):
        self._r, self._f = runner, file
        self.options = runner.options.get(proc.name) or {}
        self.features, self.work = runner.features, runner.work

    def claim(self) -> None:
        self._f.claimed = True

    def recurse(self, path: str, name: str) -> None:
        """Run the processor chain on `path` as the member `name` of this file."""
        if self._f.depth + 1 >= MAX_DEPTH:
            raise ValueError(f"archives nested more than {MAX_DEPTH} deep")
        self._r.run_file(File(path, [*self._f.address, name]))


class Runner:
    def __init__(self, sinks, procs: list, options: dict, features: list, work: str):
        self.sinks, self.procs, self.options, self.features, self.work = sinks, procs, options or {}, features or [], work

    def run_file(self, file: File) -> None:
        sink, handled = self.sinks.for_file(file.address), False
        for p in self.procs:
            if file.claimed:
                break
            try:
                if not p.accepts(file):
                    continue
            except Exception as e:  # noqa: BLE001
                sink.note(f"{p.name}: accepts failed for {file.rel} ({type(e).__name__}: {e})"[:1000])
                continue
            handled = True
            try:
                p.process(sink, file, Ctx(self, file, p))
            except Exception as e:  # noqa: BLE001  one failing processor leaves a note and the rest continue
                sink.note(f"{p.name}: {file.rel} not parsed ({type(e).__name__}: {e})"[:1000])
        if not handled:
            sink.note("not parsed (no processor)")

    def run_tree(self, root: str) -> None:
        """Every file under `root`; a file inside `<x>.d/` next to an input `<x>` is one an earlier run unpacked from it (derived)."""
        paths = sorted(os.path.relpath(os.path.join(d, n), root) for d, _, names in os.walk(root) for n in names)
        for path in paths:
            parts = path.split("/")
            derived = any(parts[i].endswith(".d") and os.path.isfile(os.path.join(root, *parts[:i], parts[i][:-2])) for i in range(len(parts) - 1))
            self.run_file(File(os.path.join(root, path), [path], derived))


def main(jobdir: str) -> None:
    job = json.load(open(os.path.join(jobdir, "in", "job.json")))
    work = os.path.join(jobdir, "work")
    os.chdir(work)  # here, as the pool uid: the wrapper itself cannot enter the directory (Popen's cwd= and preexec_fn run before the uid switch)
    sinks = PipeSinks(os.path.join(jobdir, "out"))
    try:
        procs = processors.order(processors.load())
    except processors.CycleError as e:
        sinks.fatal(str(e))
        sys.exit(2)
    Runner(sinks, procs, job.get("options"), job.get("features"), work).run_tree(os.path.join(jobdir, "in", "tree"))


if __name__ == "__main__":
    main(sys.argv[1])
