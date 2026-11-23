"""The parser wrapper: the trusted half of the parser container. It authenticates the client, pulls an input tree from a store with the
client's credential (through the egress proxy), runs the sandboxed parser on a pool uid that can read nothing and authenticate to nothing,
validates and uploads what the parser announces on its stdout pipe, and writes `index.json` and `status.json` under the output base. The
parser never sees a credential or a token other than (if the job asked for a feature) one profile's.

  POST /jobs        {input: {url, options?}, output: {url, options?}, options?, features?}  -> 202 {id, owner} | 400 | 401 | 409 | 503
  POST /jobs/renew  {id, options: {<url>: {...}}}                                          -> 200 | 400 | 401 | 404 | 410
  GET  /version, GET /health

Every non-2xx answer has a JSON body {"error": "...", "traceback"?: "..."}. There is no GET of a job: the client reads status.json and
index.json from the output base. Runs as its own uid with CAP_SETUID/SETGID/CHOWN/KILL: pool uids are UID_BASE.. . Without CAP_SETUID (tests):
one pseudo uid per slot, no setuid."""

import asyncio
import collections
import contextlib
import hmac
import json
import logging
import os
import posixpath
import resource
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

import fsspec
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cadmus_parser_server import limits as lm

log = logging.getLogger("wrapper")
API_VERSION = "1"
FEATURES = {"ocr": "OCR", "asr": "ASR", "asr-fallback": "ASR_FALLBACK", "caption": "CAPTION", "caption-fallback": "CAPTION_FALLBACK"}  # feature -> environment prefix: <PREFIX>_PROFILES (name -> {url, token}) here, <PREFIX>_URL/<PREFIX>_TOKEN in the parser
LINE_MAX = 4096  # PIPE_BUF: the parser writes each record with one write
HEARTBEAT, STALE = 10, 30  # seconds
MAX_DEPTH = 8  # archive levels in index.json
MAX_META = 2000  # bytes of one entry's `meta`, as JSON
# the operator settings the parser process enforces (no secret): the only part of the wrapper's own environment a parser gets; PARSE_PASS_ENV adds names
PARSER_ENV = ("MAX_PARSE_BYTES", "MAX_ARCHIVE_BYTES", "MAX_ARCHIVE_MEMBERS", "PARSE_MEMBER_WORKERS", "MAX_SYNTHETIC_MEMBERS", "METADATA_TIMEOUT", "METADATA_MAX_MEM", "METADATA_MAX_BYTES", "OCR_TIMEOUT",
              "OCR_LANGS", "OCR_MIN_CONF", "OCR_MIN_CHARS", "OCR_DPI", "OCR_MAX_PAGES", "OCR_MAX_SIDE", "OCR_TESSERACT_TIMEOUT", "ASR_TIMEOUT", "ASR_BUSY_WAIT", "CAPTION_TIMEOUT", "CAPTION_BUSY_WAIT")


def sidecar_call(p: dict, method: str, path: str, body: dict | None = None) -> dict:
    """One admin call to a sidecar (docs/sidecar-protocol.md, "Usage reporting"), authenticated by the profile's `admin_token`."""
    req = urllib.request.Request(p["url"].rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {p['admin_token']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310  the operator's own sidecar URL
        return json.loads(r.read())


class ProtocolError(Exception):
    """The parser sent something no honest parser sends: the job fails and the parser is killed."""


class JobError(Exception):
    """The job fails with this message as it stands (no parser fault)."""


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def discover() -> set:
    """The top-level output directories the installed processors declare, from a separate process (the wrapper imports no processor code)."""
    out = subprocess.run([sys.executable, "-I", "-m", "cadmus_parser_server.discover"], capture_output=True, check=True, timeout=120).stdout
    found = json.loads(out)
    if not found["processors"]:
        log.warning("no processor is installed: every job will leave its inputs unparsed")
    return set(found["top_dirs"])


class Config:
    def __init__(self, token: str, blob_options: dict | None = None, profiles: dict | None = None, scratch: str = "/scratch", uid_base: int = 20000,
                 pool_size: int = 4, replica: str = "wrapper", limits: dict | None = None, max_records: int = 500_000, max_notes: int = 1000,
                 max_entries: int = 50_000, parser_cmd: list | None = None, heartbeat: float = HEARTBEAT, top_dirs=None, tracebacks: bool = False,
                 pass_env: tuple = PARSER_ENV, max_usage_calls: int = 20000):
        """`profiles`: {feature: {profile: {url, token}}}. `limits_`: PARSE_LIMITS (lm.resolve). `top_dirs`: default = what the processors declare."""
        self.token, self.blob_options, self.profiles = token, blob_options or {}, profiles or {}
        self.scratch, self.uid_base, self.pool_size, self.replica = scratch, uid_base, pool_size, replica
        self.limits = lm.resolve(limits)
        self.max_records, self.max_notes, self.max_entries = max_records, max_notes, max_entries
        self.parser_cmd = parser_cmd or [sys.executable, "-I", "-u", "-m", "cadmus_parser_server.worker"]
        self.heartbeat, self.tracebacks, self.pass_env = heartbeat, tracebacks, pass_env
        self.max_usage_calls = max_usage_calls
        self.top_dirs = set(top_dirs) if top_dirs is not None else None

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        return cls(e["WRAPPER_TOKEN"], json.loads(e.get("PARSE_BLOB_OPTIONS") or "{}"),
                   {f: json.loads(e.get(f"{p}_PROFILES") or "{}") for f, p in FEATURES.items()}, e.get("PARSE_SCRATCH", "/scratch"),
                   env_int("PARSE_UID_BASE", 20000), env_int("PARSE_POOL_SIZE", 4), e.get("REPLICA_ID") or e.get("HOSTNAME") or "wrapper",
                   json.loads(e.get("PARSE_LIMITS") or "{}"), max_entries=env_int("MAX_ARCHIVE_MEMBERS", 50_000),
                   tracebacks=e.get("PARSE_TRACEBACKS") == "1", pass_env=(*PARSER_ENV, *e.get("PARSE_PASS_ENV", "").split()),
                   max_usage_calls=env_int("PARSE_USAGE_MAX_CALLS", 20000))


# ---- the uid pool ---------------------------------------------------------------------------------------------

def _reclaim(path: str, uid: int) -> None:
    """Make a tree the wrapper's own again (the parser uid owned it): lchown, and owner-rwx on directories so rmtree can enter."""
    with contextlib.suppress(PermissionError):  # chown needs CAP_CHOWN; without it (tests) the tree is already ours
        os.lchown(path, os.geteuid(), os.getegid())
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        os.chmod(path, 0o700)
        for e in os.scandir(path):
            _reclaim(e.path, uid)


def owned_by(uid: int, roots) -> str | None:
    """First path under `roots` owned by `uid` (None if there is none): a pool uid is only freed when it owns nothing."""
    for root in roots:
        stack = [root]
        while stack:
            p = stack.pop()
            try:
                st = os.lstat(p)
                if st.st_uid == uid:
                    return p
                if stat.S_ISDIR(st.st_mode):
                    stack.extend(e.path for e in os.scandir(p))
            except OSError:
                continue
    return None


def purge_owned(uid: int, roots) -> None:
    """Delete what a pool uid left in shared places (/tmp ...): lchown it to ourselves first (CAP_CHOWN), then it is ours to remove."""
    for _ in range(1000):
        if not (p := owned_by(uid, roots)):
            return
        _reclaim(p, uid)
        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)


def drop_ambient() -> None:
    """For a child's preexec_fn: the wrapper's capabilities are ambient, and an ambient capability survives setuid() and exec."""
    import ctypes
    ctypes.CDLL(None, use_errno=True).prctl(47, 4, 0, 0, 0)  # PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL


def kill_uid(uid: int) -> None:
    """Kill every process of `uid` (also those that left the process group): a helper running as the uid signals everything it may signal."""
    subprocess.run([sys.executable, "-I", "-c", "import os,signal; os.kill(-1, signal.SIGKILL)"], user=uid, group=uid, extra_groups=[], preexec_fn=drop_ambient,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def can_setuid() -> bool:
    """The wrapper runs as its own uid with CAP_SETUID (not as root): that, not euid 0, decides whether pool uids are real."""
    try:
        caps = next(int(l.split()[1], 16) for l in open("/proc/self/status") if l.startswith("CapEff"))
    except (OSError, StopIteration):
        return os.geteuid() == 0
    return bool(caps & (1 << 7))  # CAP_SETUID


class Pool:
    """Slots `free | busy | quarantined`; a slot is a uid (root) or a scratch dir run as the wrapper's own uid (tests)."""

    def __init__(self, cfg: Config):
        self.cfg, self.lock = cfg, threading.Lock()
        self.state = {i: "free" for i in range(cfg.pool_size)}
        self.real = can_setuid()
        os.makedirs(cfg.scratch, exist_ok=True)
        os.chmod(cfg.scratch, 0o711)

    def uid(self, slot: int) -> int:
        return self.cfg.uid_base + slot if self.real else os.geteuid()

    def dir(self, slot: int) -> str:
        return os.path.join(self.cfg.scratch, f"slot-{slot}")

    def acquire(self) -> int | None:
        with self.lock:
            for slot, s in self.state.items():
                if s == "free":
                    self.state[slot] = "busy"
                    return slot
        return None

    def release(self, slot: int, quarantine: bool = False) -> None:
        with self.lock:
            self.state[slot] = "quarantined" if quarantine else "free"

    def clean(self, slot: int, proc=None) -> bool:
        """Kill, wipe and verify; False = the slot must be quarantined."""
        d, uid = self.dir(slot), self.uid(slot)
        try:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            if self.real:
                kill_uid(uid)
            if os.path.lexists(d):
                _reclaim(d, uid)
                shutil.rmtree(d)
            roots = [self.cfg.scratch, "/tmp", "/var/tmp", "/dev/shm"]
            if self.real:
                purge_owned(uid, roots)
            left = owned_by(uid, roots) if self.real else None
            if left:
                log.error("uid %s still owns %s: quarantined", uid, left)
            return left is None
        except Exception:  # noqa: BLE001
            log.exception("wipe of slot %s failed: quarantined", slot)
            return False



# ---- one job --------------------------------------------------------------------------------------------------

def open_nofollow(root: str, rel: str) -> tuple[int, int, str]:
    """(fd, parent dir fd, name) of the regular file root/rel with no symlink followed in any component."""
    *dirs, name = rel.split("/")
    dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for d in dirs:
            nfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd), dfd, name  # NONBLOCK: a fifo must not hang us
    except BaseException:
        os.close(dfd)
        raise


def valid_rel(rel, first) -> str:
    if (not isinstance(rel, str) or not rel or len(rel) > 1024 or "\0" in rel or "\\" in rel or rel.startswith("/")
            or os.path.normpath(rel) != rel or rel.split("/")[0] in ("", ".", "..") or rel.split("/")[0] not in first or "/" not in rel):
        raise ProtocolError(f"bad path {str(rel)[:80]!r}")
    return rel


def drain_stderr(proc, cap: int = 64 << 10) -> None:
    """The parser's stderr goes to the wrapper's log, capped, never to the client."""
    n = 0
    for line in proc.stderr:
        if n < cap:
            log.info("parser: %s", line.decode("utf-8", "replace").rstrip()[:500])
        n += len(line)


class Index:
    """index.json under construction: one entry per input file, recursive for archive members, built from the parser's records."""

    def __init__(self, inputs: dict, max_entries: int):
        self.files = {rel: {"size": size, "outputs": {}} for rel, size in inputs.items()}
        self.entries, self.max_entries, self.notes = len(inputs), max_entries, []

    def entry(self, address) -> dict | None:
        """The entry of `address`, created on demand below the first element; None (and a note) past the depth or entry cap."""
        if not (isinstance(address, list) and 0 < len(address) <= 1000 and all(isinstance(a, str) and a and len(a) <= 1024 and "\0" not in a for a in address)):
            raise ProtocolError("bad input address")
        if address[0] not in self.files:
            raise ProtocolError(f"unknown input {address[0][:80]!r}")
        if len(address) > MAX_DEPTH:
            return self._cap(f"archives nested more than {MAX_DEPTH} deep: not listed")
        e = self.files[address[0]]
        for name in address[1:]:
            members = e.setdefault("files", {})
            if name not in members:
                if self.entries >= self.max_entries:
                    return self._cap(f"more than {self.max_entries} entries: the rest is not listed")
                members[name] = {"size": 0, "outputs": {}}
                self.entries += 1
            e = members[name]
        return e

    def _cap(self, note: str):
        if note not in self.notes:
            self.notes.append(note)
        return None


class Side:
    """One end of a job: a base URL and the credential for it, replaceable while the job runs (the client renews it)."""

    def __init__(self, w: "Wrapper", url: str, options: dict):
        self.w, self.url, self._options, self.lock = w, url, options, threading.Lock()

    def set(self, options: dict) -> None:
        with self.lock:
            self._options = options

    def fs(self):
        """A filesystem with the newest credential and the path of the base in it; one per file transfer, never kept."""
        with self.lock:
            opts = self._options
        return self.w.fs(self.url, opts)


class Job:
    def __init__(self, w: "Wrapper", slot: int, body: dict):
        self.w, self.slot, self.req, self.id = w, slot, body, secrets.token_urlsafe(16)
        self.input = Side(w, body["input"]["url"], body["input"].get("options") or {})
        self.output = Side(w, body["output"]["url"], body["output"].get("options") or {})
        self.key = body["output"]["url"].rstrip("/")  # the job's identity
        self.timed_out, self.total, self.notes, self.paths, self.worker_error = False, 0, [], set(), None
        self.reg_errors: list = []
        self.tokens: dict = {}  # feature -> job token, for sidecars that report usage

    # -- status --
    def write_json(self, name: str, obj) -> None:
        fs, base = self.output.fs()
        with fs.open(f"{base.rstrip('/')}/{name}", "wb") as f:
            f.write(json.dumps(obj).encode())

    def status(self, status: str, error: str | None = None, tb: str | None = None, usage: dict | None = None) -> None:
        s = {"status": status, "owner_replica": self.w.cfg.replica, "updated": time.time(), **({"error": error} if error else {}),
             **({"traceback": tb} if error and tb and self.w.cfg.tracebacks else {})}
        self.w.remember(self.key, s)
        with contextlib.suppress(Exception):  # a heartbeat that cannot be written must not kill the parse
            self.write_json("status.json", {**s, "usage": usage} if usage else s)  # usage is not remembered: it can be big

    # -- usage reporting: the parser gets a job token, the sidecar records against it, the wrapper reads the record --
    def register(self, lim: dict) -> None:  # one sidecar record per feature (id `<job>.<feature>`): two features on one sidecar must not share a record
        ttl = (lim["deadline_s"] or 3600) + 300
        for feature, profile in (self.req.get("features") or {}).items():
            p = self.w.cfg.profiles[feature][profile]
            if p.get("admin_token"):
                try:
                    self.tokens[feature] = sidecar_call(p, "POST", "/admin/jobs", {"job": f"{self.id}.{feature}", "profile": profile, "ttl": ttl})["token"]
                except Exception as e:  # noqa: BLE001  no usage for this feature; the parser keeps the profile token and the job runs
                    log.warning("could not register job %s with %s: %s", self.key, feature, e)
                    self.reg_errors.append(f"{feature}: {type(e).__name__}")

    def collect(self) -> dict | None:
        calls, truncated, errors, cap = [], False, list(self.reg_errors), self.w.cfg.max_usage_calls
        for feature in self.tokens:
            p = self.w.cfg.profiles[feature][self.req["features"][feature]]
            try:
                r = sidecar_call(p, "GET", f"/admin/jobs/{self.id}.{feature}/usage")
                calls += r["calls"]
                truncated |= bool(r.get("truncated"))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{feature}: {type(e).__name__}")
        if not self.tokens and not errors:
            return None
        calls.sort(key=lambda c: c.get("t", 0))
        return {"calls": calls[:cap], "truncated": truncated or len(calls) > cap, **({"error": "; ".join(errors)} if errors else {})}

    # -- steps --
    def listing(self) -> dict:
        """{relative path: size} of everything under the input base (a single object is a one-file tree), checked against the operator's ceiling."""
        fs, base = self.input.fs()
        base = base.rstrip("/")
        try:
            found = fs.find(base, detail=True)
        except PermissionError as e:
            raise JobError(f"cannot list the input base: the credential needs list permission ({type(e).__name__})") from None
        out, self.single = {}, base in found
        if self.single:
            out[posixpath.basename(base)] = found[base]["size"]
        else:
            for p, info in found.items():
                rel = p[len(base) + 1:] if p.startswith(base + "/") else None
                if info.get("type") != "file" or not rel or os.path.normpath(rel) != rel or rel.startswith("..") or "\0" in rel:
                    continue
                out[rel] = info["size"]
        if (cap := lm.ceiling(self.w.cfg.limits, "input_mib")) and sum(out.values()) > lm.mib(cap):
            raise JobError(f"input too big ({sum(out.values()) >> 20} MiB > {cap} MiB)")
        return out

    def pull(self, indir: str, names: dict) -> dict:
        """The input tree -> indir/tree/; returns {rel: size} from the files as written here."""
        sizes = {}
        os.makedirs(os.path.join(indir, "tree"))
        for rel in names:
            fs, base = self.input.fs()  # per transfer: the newest credential
            src = base.rstrip("/") if self.single else f"{base.rstrip('/')}/{rel}"
            dest = os.path.join(indir, "tree", rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            fs.get_file(src, dest)
            sizes[rel] = os.lstat(dest).st_size
        with open(os.path.join(indir, "job.json"), "w") as f:
            json.dump({"options": self.req.get("options") or {}, "features": sorted(self.req.get("features") or {})}, f)
        return sizes

    def env(self, work: str) -> dict:
        cfg = self.w.cfg
        env = {"PYTHONUNBUFFERED": "1", "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": work, "TMPDIR": work,
               "LANG": "C.UTF-8", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        env |= {k: os.environ[k] for k in cfg.pass_env if os.environ.get(k)}
        for feature, profile in (self.req.get("features") or {}).items():  # only the requested profile: a job never holds another's token
            p, prefix = cfg.profiles[feature][profile], FEATURES[feature]
            env |= {f"{prefix}_URL": p["url"], f"{prefix}_TOKEN": self.tokens.get(feature, p["token"])}
        return env

    def launch(self, jobdir: str, uid: int, lim: dict) -> subprocess.Popen:
        cfg = self.w.cfg
        inf = resource.RLIM_INFINITY

        def val(n, scale=1):
            return inf if not lim[n] else lm.mib(lim[n]) if scale else int(lim[n])

        def limit_fn():
            # NPROC counts a uid's threads, and is per uid: meaningless when the parser shares the wrapper's uid (tests)
            for r, v in ((resource.RLIMIT_AS, val("ram_mib")), *([(resource.RLIMIT_NPROC, val("nproc", 0))] if self.w.pool.real else []),
                         (resource.RLIMIT_NOFILE, val("nofile", 0)), (resource.RLIMIT_FSIZE, val("fsize_mib")), (resource.RLIMIT_CORE, 0)):
                resource.setrlimit(r, (v, v))
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS
            drop_ambient()  # the parser must hold no capability at all

        extra = {"user": uid, "group": uid, "extra_groups": []} if self.w.pool.real else {}
        return subprocess.Popen([*cfg.parser_cmd, jobdir], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.env(os.path.join(jobdir, "work")), close_fds=True,
                                start_new_session=True, umask=0o007, preexec_fn=limit_fn, **extra)

    def upload(self, out: str, rel: str, entry: dict, ctype: str, meta, cap: int) -> None:
        fd, dfd, name = open_nofollow(out, rel)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ProtocolError(f"{rel} is not a regular file")
            self.total += st.st_size  # the size comes from our own fstat, never from the record
            if cap and self.total > cap:
                raise ProtocolError("output too big")
            fs, base = self.output.fs()  # per transfer: the newest credential
            with os.fdopen(fd, "rb", closefd=False) as src, fs.open(f"{base.rstrip('/')}/{rel}", "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            os.unlink(name, dir_fd=dfd)
        finally:
            os.close(fd)
            os.close(dfd)
        entry["outputs"][rel] = {"size": st.st_size, "content_type": ctype, **({"meta": meta} if meta else {})}

    @staticmethod
    def _meta(meta):
        if meta is None:
            return None
        if not isinstance(meta, dict) or len(json.dumps(meta)) > MAX_META:
            raise ProtocolError("meta is not a small object")
        return meta

    @staticmethod
    def _ctype(c):
        return c[:200] if isinstance(c, str) and c else None

    def note(self, entry: dict | None, text) -> None:
        if not isinstance(text, str) or len(self.notes) >= self.w.cfg.max_notes:
            return
        self.notes.append(text)
        text = text[:1000]
        if entry is None:
            self.index.notes.append(text)
        else:
            entry["notes"] = (entry["notes"] + "\n" if entry.get("notes") else "") + text

    def record(self, out: str, line: bytes, lim: dict) -> None:
        try:
            rec = json.loads(line)
        except ValueError:
            raise ProtocolError("record is not JSON") from None
        if not isinstance(rec, dict):
            raise ProtocolError("record is not an object")
        if "error" in rec:  # the worker could not start (a processor cycle): the reason of the failure
            self.worker_error = str(rec["error"])[:1000]
            return
        if "note" in rec and "in" not in rec:  # a remark of the wrapper-level index.json
            return self.note(None, rec["note"])
        if "info" in rec or "note" in rec:
            entry = self.index.entry(rec.get("in"))
            if "note" in rec:
                return self.note(entry, rec["note"]) if entry is not None else None
            info = rec["info"]
            if not isinstance(info, dict):
                raise ProtocolError("info is not an object")
            ctype, meta = self._ctype(info.get("content_type")), self._meta(info.get("meta"))
            if entry is not None:
                if ctype:
                    entry["content_type"] = ctype
                if meta:
                    entry["meta"] = {**entry.get("meta", {}), **meta}
                if len(rec["in"]) > 1 and isinstance(info.get("size"), int) and info["size"] >= 0:
                    entry["size"] = info["size"]  # only a member's size is the processor's claim: an input's is our own
            return
        rel = valid_rel(rec.get("path"), self.w.top_dirs)  # an output of an input: {in, path, content_type?, meta?}
        meta, ctype = self._meta(rec.get("meta")), self._ctype(rec.get("content_type")) or "application/octet-stream"
        if (entry := self.index.entry(rec.get("in"))) is None:
            return
        if rel in self.paths:
            return self.note(entry, f"output {rel} written twice: the later write is refused")
        self.paths.add(rel)
        self.upload(out, rel, entry, ctype, meta, lm.mib(lim["output_mib"]))

    def finish(self, error: str | None, tb: str | None = None) -> None:
        """index.json first (artefacts already uploaded stay), then status.json."""
        index = {"files": self.index.files, **({"notes": "\n".join(self.index.notes)} if self.index.notes else {})}
        self.write_json("index.json", index)
        self.status("failed" if error else "done", error, tb, self.collect())

    def run(self) -> None:
        w, error, tb, proc = self.w, None, None, None
        jobdir, uid = w.pool.dir(self.slot), w.pool.uid(self.slot)
        stop, beat, timer = threading.Event(), None, None
        self.index = Index({}, w.cfg.max_entries)
        try:
            def heartbeat():
                while not stop.wait(w.cfg.heartbeat):
                    self.status("running")
            beat = threading.Thread(target=heartbeat, daemon=True)
            beat.start()
            w.pool.clean(self.slot)
            os.mkdir(jobdir, 0o700)
            for d in ("in", "out", "work"):
                os.mkdir(os.path.join(jobdir, d), 0o700)
            names = self.listing()
            sizes = self.pull(os.path.join(jobdir, "in"), names)
            self.index = Index(sizes, w.cfg.max_entries)
            tree = os.path.join(jobdir, "in", "tree")
            classes = {lm.classify(os.path.join(tree, rel), rel) for rel in sizes}
            lim = lm.merge(w.cfg.limits, classes)
            if lim["input_mib"] and sum(sizes.values()) > lm.mib(lim["input_mib"]):
                raise JobError(f"input too big for its class ({sum(sizes.values()) >> 20} MiB > {lim['input_mib']} MiB)")
            if w.pool.real:
                # list everything first and chown the job dir last: once a directory is the uid's, the wrapper cannot reach what is in it
                paths = [jobdir] + [os.path.join(d, x) for d, dirs, names_ in os.walk(jobdir) for x in dirs + names_]
                out = os.path.join(jobdir, "out")
                # the wrapper has no DAC_OVERRIDE: it reaches the artefacts through its group (setgid out/, parser umask 007), nothing else of the uid's
                os.chmod(jobdir, 0o710)  # before the chown: afterwards the wrapper is not the owner
                os.chmod(out, 0o2770)
                for n in reversed(paths):
                    os.lchown(n, uid, os.getegid() if n in (jobdir, out) else uid)
            self.register(lim)
            proc = self.launch(jobdir, uid, lim)
            threading.Thread(target=drain_stderr, args=(proc,), daemon=True).start()

            def reap():  # whatever the parser left running (a setsid'ed child holding the pipe) must not outlive it: EOF would never come
                proc.wait()
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                if w.pool.real:
                    kill_uid(uid)
            threading.Thread(target=reap, daemon=True).start()

            def expire():
                self.timed_out = True
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
            if lim["deadline_s"]:
                timer = threading.Timer(lim["deadline_s"], expire)
                timer.start()
            out, count, clean_eof = os.path.join(jobdir, "out"), 0, True
            while line := proc.stdout.readline(LINE_MAX + 1):
                if len(line) > LINE_MAX or not line.endswith(b"\n"):
                    clean_eof = False  # a too-long line, or a record cut off by a kill
                    if len(line) > LINE_MAX:
                        raise ProtocolError("record too long")
                    break
                count += 1
                if count > w.cfg.max_records:
                    raise ProtocolError("too many records")
                self.record(out, line, lim)
            rc = proc.wait()
            if self.timed_out:
                error = f"parser timed out after {lim['deadline_s']} s"
            elif rc != 0:
                error = f"parser exited with {rc}" + (f": {self.worker_error}" if self.worker_error else "")
            elif not clean_eof:
                error = "parser output truncated"
        except ProtocolError as e:
            error = f"parser protocol violation: {e}"
        except JobError as e:
            error = str(e)
        except Exception as e:  # noqa: BLE001  a full disk, a pull error or a store error fails this job only
            log.exception("job %s failed", self.key)
            error, tb = f"{type(e).__name__}: {str(e)[:200]}", traceback.format_exc()
        finally:
            if timer:
                timer.cancel()
            stop.set()
            if beat:
                beat.join()
            ok = w.pool.clean(self.slot, proc)  # after the process group is dead, before the uid is freed again
            w.pool.release(self.slot, quarantine=not ok)  # before the result: a client that re-POSTs the moment it sees `done` must find the slot free
            try:
                self.finish(error, tb)
            except Exception:  # noqa: BLE001
                log.exception("could not write the result of %s", self.key)
            w.done(self)


class Wrapper:
    def __init__(self, cfg: Config):
        self.cfg, self.pool = cfg, Pool(cfg)
        self.top_dirs = cfg.top_dirs if cfg.top_dirs is not None else discover()
        self.memory: collections.OrderedDict = collections.OrderedDict()  # output url -> last status
        self.jobs: dict = {}  # id -> running Job
        self.finished: collections.OrderedDict = collections.OrderedDict()  # ids of finished jobs, for the 410

    def remember(self, key: str, state: dict) -> None:
        self.memory[key] = state
        self.memory.move_to_end(key)
        while len(self.memory) > 1000:
            self.memory.popitem(last=False)

    def done(self, job: Job) -> None:
        self.jobs.pop(job.id, None)
        self.finished[job.id] = True
        while len(self.finished) > 1000:
            self.finished.popitem(last=False)

    def fs(self, url: str, creds: dict):
        """(filesystem, path of the base in it) for a URL and the options of that side, merged over the operator's. fsspec gets everything as given."""
        creds = dict(creds)
        if "gcs_access_token" in creds:  # GCS: a downscoped access token travels as a string
            from google.oauth2.credentials import Credentials
            creds["token"] = Credentials(creds.pop("gcs_access_token"))
        opts = {**self.cfg.blob_options, **creds, "skip_instance_cache": True}
        if url.startswith(("gs://", "gcs://")):
            opts["session_kwargs"] = {"trust_env": True}  # gcsfs (aiohttp) ignores HTTP(S)_PROXY, the only way out of this container, unless told to look
        return fsspec.core.url_to_fs(url, use_listings_cache=False, **opts)

    def read_status(self, url: str, creds: dict) -> dict:
        key = url.rstrip("/")
        s = self.memory.get(key)
        if s is None:
            fs, base = self.fs(url, creds)
            try:
                s = json.loads(fs.cat_file(f"{base.rstrip('/')}/status.json"))
            except FileNotFoundError:
                return {"status": "not_started"}
        if s["status"] == "running" and time.time() - s["updated"] > STALE:
            return {"status": "failed", "error": "wrapper lost"}
        return s


def error(status: int, msg: str, tb: str | None = None) -> JSONResponse:
    return JSONResponse({"error": msg, **({"traceback": tb} if tb else {})}, status)


def side_of(body: dict, name: str):
    s = body.get(name)
    ok = isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"] and isinstance(s.get("options") or {}, dict)
    if not ok:
        raise ValueError(f"{name} needs a url (and optional options object)")
    return s


def make_app(cfg: Config) -> Starlette:
    w = Wrapper(cfg)

    def denied(request: Request):
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth.encode(), f"Bearer {cfg.token}".encode()):
            return error(401, "unauthorized")

    async def post(request: Request):
        if r := denied(request):
            return r
        try:
            body = await request.json()
            side_of(body, "input"), side_of(body, "output")
            features, options = body.get("features") or {}, body.get("options") or {}
            assert isinstance(features, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in features.items())
            assert isinstance(options, dict) and all(isinstance(v, dict) for v in options.values())
        except Exception as e:  # noqa: BLE001
            return error(400, f"bad request body ({type(e).__name__}: {str(e)[:100]})" if isinstance(e, ValueError) else "bad request body")
        if unknown := sorted(set(features) - set(FEATURES)):
            return error(400, f"unknown feature {unknown[0]!r}")
        for feature, profile in features.items():
            if profile not in cfg.profiles.get(feature, {}):
                return error(400, f"unknown {feature} profile")
        try:
            cur = await asyncio.to_thread(w.read_status, body["output"]["url"], body["output"].get("options") or {})  # a stale heartbeat reads as failed: restarted
        except Exception as e:  # noqa: BLE001
            return error(400, f"storage credential rejected ({type(e).__name__})")
        if cur["status"] == "running":
            return error(409, "job is running")
        if (slot := w.pool.acquire()) is None:
            return error(503, "no free parser")
        try:
            job = Job(w, slot, body)
            await asyncio.to_thread(job.status, "running")
            fs, base = job.output.fs()  # the status of a job nobody can read is not a job: the output base must be writable now
            await asyncio.to_thread(fs.cat_file, f"{base.rstrip('/')}/status.json")
        except BaseException as e:
            w.pool.release(slot)
            w.memory.pop(body["output"]["url"].rstrip("/"), None)
            if isinstance(e, Exception):
                return error(400, f"cannot write to the output base ({type(e).__name__})")
            raise
        w.jobs[job.id] = job
        threading.Thread(target=job.run, daemon=True).start()
        host, port = request.scope.get("server") or ("", 0)  # the address this request arrived on: where the client reaches this replica
        return JSONResponse({"status": "running", "id": job.id, "owner": f"[{host}]:{port}" if ":" in host else f"{host}:{port}"}, 202)

    async def renew(request: Request):
        if r := denied(request):
            return r
        try:
            body = await request.json()
            jid, options = body["id"], body["options"]
            assert isinstance(jid, str) and isinstance(options, dict) and all(isinstance(v, dict) for v in options.values())
        except Exception:  # noqa: BLE001
            return error(400, "bad request body")
        if (job := w.jobs.get(jid)) is None:
            return error(410, "job finished") if jid in w.finished else error(404, "unknown job")
        for url in options:
            if url not in (job.input.url, job.output.url):
                return error(400, "options key matches neither the input nor the output url")
        for side in (job.input, job.output):
            if side.url in options:
                side.set(options[side.url])
        return JSONResponse({"status": "renewed"})

    async def failure(request: Request, exc: Exception):
        if isinstance(exc, HTTPException):
            return error(exc.status_code, exc.detail)
        log.exception("unhandled error")
        return error(500, f"{type(exc).__name__}: {str(exc)[:200]}", traceback.format_exc() if cfg.tracebacks else None)

    app = Starlette(routes=[Route("/jobs", post, methods=["POST"]), Route("/jobs/renew", renew, methods=["POST"]),
                            Route("/version", lambda r: JSONResponse({"api": API_VERSION})), Route("/health", lambda r: JSONResponse({"ok": True}))],
                    exception_handlers={HTTPException: failure, Exception: failure})
    app.state.wrapper = w
    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    if token := os.environ.get("PROXY_TOKEN"):  # only the wrapper's own storage traffic uses the proxy; the parser env is built by hand
        url = f"http://parse:{token}@{os.environ['PARSE_PROXY_HOST']}"
        os.environ.update(HTTP_PROXY=url, HTTPS_PROXY=url, http_proxy=url, https_proxy=url)
    uvicorn.run(make_app(Config.from_env()), host="0.0.0.0", port=env_int("PARSE_PORT", 8080))


if __name__ == "__main__":
    main()
