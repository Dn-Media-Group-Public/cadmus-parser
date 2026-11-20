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
import threading  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from cadmus_parser_server import processors  # noqa: E402

MAX_DEPTH = 8
MAX_SYNTHETIC_MEMBERS = int(os.environ.get("MAX_SYNTHETIC_MEMBERS") or 10000)
MEMBER_WORKERS = int(os.environ.get("PARSE_MEMBER_WORKERS") or 4)
LINE_MAX = 4096  # PIPE_BUF: one write per record is atomic


class Sink:
    """Where one input file's artefacts go. `rel` is a path relative to the output base, below a top_dir some processor declared."""

    def put_bytes(self, rel: str, data: bytes, ctype: str, meta: dict | None = None) -> None: ...
    def put_file(self, rel: str, src: str, ctype: str, meta: dict | None = None) -> None: ...  # `src` (a file in the work dir) also goes to the output; it stays in the work dir
    def info(self, content_type: str | None = None, meta: dict | None = None, size: int | None = None) -> None: ...  # about the input itself
    def note(self, text: str) -> None: ...


class PipeSinks:
    """One per job: the sinks of its input files, writing to the protocol pipe. An output path is announced once per job: a second write
    could be deleted by the wrapper after its upload, so it is refused and noted on the input that asked."""

    def __init__(self, out: str):
        self.out, self.sent, self.lock = out, set(), threading.Lock()

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
        with self.s.lock:
            fresh = rel not in self.s.sent
            self.s.sent.add(rel)
        if not fresh:
            self.note(f"output {rel} written twice: the later write is refused")
        return fresh

    def _put(self, rel, ctype, meta):
        self.s.announce({"in": self.address, "path": rel, "content_type": ctype, **({"meta": meta} if meta else {})})

    def put_bytes(self, rel, data, ctype, meta=None):
        if self._fresh(rel):
            with open(self._dest(rel), "wb") as f:
                f.write(data)
            self._put(rel, ctype, meta)

    def put_file(self, rel, src, ctype, meta=None):
        if not self._fresh(rel):
            return
        dest = self._dest(rel)
        try:  # a hard link: no second copy of a large file. The inode gets out/'s group, which is how the wrapper reads it
            os.link(src, dest)
            os.chown(dest, -1, os.stat(self.s.out).st_gid)
            os.chmod(dest, os.stat(dest).st_mode | 0o040)
        except OSError:
            if os.path.lexists(dest):
                os.unlink(dest)
            shutil.copyfile(src, dest)  # the new file takes out/'s group (setgid, umask 007)
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

    def __init__(self, path: str, address: list, derived: bool = False, index_address: list | None = None):
        self.path, self.address, self.derived = path, address, derived
        self.index_address = index_address  # the entry reported on; a synthetic member reports on its nearest real ancestor's
        self.name, self.depth, self.rel = address[-1], len(address) - 1, ".d/".join(address)
        self.claimed_by = None
        self.size = os.path.getsize(path)

    @property
    def claimed(self) -> bool:
        return self.claimed_by is not None


class Member:
    """The result of `ctx.recurse`: `address`, `claimed_by` (processor name or None), `outputs` (paths announced for it, relative) and `dir(processor)`."""

    def __init__(self, runner: "Runner", file: File, outputs: list):
        self._r, self.address, self.claimed_by, self.outputs, self.rel = runner, file.address, file.claimed_by, outputs, file.rel

    def dir(self, processor: str) -> str:
        return self._r.dir(processor, self.rel)


class _Rec(Sink):
    """Wraps the sink of a file: records the outputs it announces; a synthetic member's `info` is dropped (it would overwrite its ancestor's)."""

    def __init__(self, sink: Sink, synthetic: bool):
        self.sink, self.synthetic, self.outputs = sink, synthetic, []

    def put_bytes(self, rel, data, ctype, meta=None):
        self.outputs.append(rel)
        self.sink.put_bytes(rel, data, ctype, meta)

    def put_file(self, rel, src, ctype, meta=None):
        self.outputs.append(rel)
        self.sink.put_file(rel, src, ctype, meta)

    def info(self, content_type=None, meta=None, size=None):
        if not self.synthetic:
            self.sink.info(content_type, meta, size)

    def note(self, text):
        self.sink.note(text)


class Ctx:
    """What a processor gets besides the sink and the file. `claim()` stops the later processors for this file."""

    def __init__(self, runner: "Runner", file: File, proc):
        self._r, self._f, self._p = runner, file, proc
        self.options = runner.options.get(proc.name) or {}
        self.features, self.work = runner.features, runner.work

    def options_of(self, name: str) -> dict:
        """The job's options for another name: a family of processors may share one entry (the default ones read `files`)."""
        return self._r.options.get(name) or {}

    def claim(self) -> None:
        self._f.claimed_by = self._p.name

    def dir(self) -> str:
        """This processor's directory for this file under the kept scratch (created); later processors read it."""
        return self._r.dir(self._p.name, self._f.rel, create=True)

    def _member(self, path: str, name: str, synthetic: bool) -> File:
        if self._f.depth + 1 >= MAX_DEPTH:
            raise ValueError(f"archives nested more than {MAX_DEPTH} deep")
        if synthetic:
            self._r.count_synthetic()
        return File(path, [*self._f.address, name], index_address=(self._f.index_address or self._f.address) if synthetic else None)

    def recurse(self, path: str, name: str, synthetic: bool = False) -> Member:
        """Run the processor chain on `path` as the member `name` of this file. A synthetic member (a chunk, a window) is not listed in the index:
        its notes and outputs land on the nearest real ancestor's entry."""
        return self._r.run_file(self._member(path, name, synthetic))

    def recurse_many(self, items: list, workers: int = 4, synthetic: bool = False) -> list:
        """`recurse` for [(path, name)] in a thread pool; the Members in input order. Serial if any processor declares `threadsafe = False`."""
        files = [self._member(p, n, synthetic) for p, n in items]
        workers = max(1, min(workers, MEMBER_WORKERS))
        if not all(getattr(p, "threadsafe", True) for p in self._r.procs):
            workers = 1
        with ThreadPoolExecutor(workers) as pool:
            return list(pool.map(self._r.run_file, files))


class Runner:
    def __init__(self, sinks, procs: list, options: dict, features: list, work: str):
        self.sinks, self.procs, self.options, self.features, self.work = sinks, procs, options or {}, features or [], work
        self.synthetic, self.lock = 0, threading.Lock()

    def count_synthetic(self) -> None:
        with self.lock:
            self.synthetic += 1
            if self.synthetic > MAX_SYNTHETIC_MEMBERS:
                raise ValueError(f"more than {MAX_SYNTHETIC_MEMBERS} synthetic members")

    def dir(self, processor: str, rel: str, create: bool = False) -> str:
        d = os.path.join(self.work, processor, rel)
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    def run_file(self, file: File) -> Member:
        synthetic = file.index_address is not None
        sink, handled = _Rec(self.sinks.for_file(file.index_address if synthetic else file.address), synthetic), False
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
        return Member(self, file, sink.outputs)

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
