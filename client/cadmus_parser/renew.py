"""Keep long jobs' credentials alive: the client pushes. Every `interval` seconds a loop mints fresh credentials and sends them to the replica that owns the
job. **One shared thread serves every running job**: it starts with the first `renew_loop` and ends when the last one stops. A renewal blocks the
thread for the length of its HTTP call (a few seconds at worst with retries), which a 5-minute cadence does not feel. Stopping the pushes ends a job's
access when its current credential expires."""

import logging
import threading
import time
from collections.abc import Mapping

import httpx

from cadmus_parser.minting import mint

log = logging.getLogger("cadmus_parser.renew")


class _Scheduler:
    def __init__(self):
        self.cv, self.loops, self.thread = threading.Condition(), set(), None

    def add(self, loop: "RenewLoop") -> None:
        with self.cv:
            loop.due = time.monotonic() + loop.interval
            self.loops.add(loop)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="cadmus-renew")
                self.thread.start()
            self.cv.notify()

    def remove(self, loop: "RenewLoop") -> None:
        with self.cv:
            self.loops.discard(loop)
            self.cv.notify()

    def _run(self) -> None:
        while True:
            with self.cv:
                while True:
                    if not self.loops:
                        self.thread = None  # the last job is done: the thread ends, the next job starts a new one
                        return
                    loop = min(self.loops, key=lambda lp: lp.due)
                    wait = loop.due - time.monotonic()
                    if wait <= 0:
                        break
                    self.cv.wait(wait)
            try:
                loop.step()
            except Exception:  # noqa: BLE001  one job's trouble never ends the thread
                log.exception("renewal of %s", loop.id)
                self.remove(loop)
            with self.cv:
                loop.due = time.monotonic() + loop.interval


_scheduler = _Scheduler()


class RenewLoop:
    """`stop()` ends the renewals and revokes every credential the loop minted and `first`. `lost` is True when the job's replica is gone (404 or
    unreachable), `finished` when it answered 410; the client then marks the job failed ("wrapper lost") or lets the status say. `error` says why."""

    def __init__(self, client, grants, job_id: str, owner: str, first=None, interval: float = 300, lifetime: int = 900, options: dict | None = None,
                 retries: int = 3, retry_delay: float = 1, rekey: Mapping | None = None, on_mint=None):
        """`rekey` = {grant url: posted url (or a list of them)} when a grant was wider than a posted url (see Client.renew). `on_mint(handle)` is called with
        every handle the loop mints, for a client that keeps a registry of live credentials."""
        self.client, self.grants, self.id, self.owner, self.rekey, self.on_mint = client, list(grants), job_id, owner, rekey, on_mint
        self.interval, self.lifetime, self.options, self.retries, self.retry_delay = interval, lifetime, options, retries, retry_delay
        self.handles = [first] if first is not None else []
        self.lost = self.finished = self.stopped = False
        self.error: str | None = None
        self.due = 0.0
        self._busy = threading.Lock()
        _scheduler.add(self)

    @property
    def current(self):
        """The newest handle: what a reader of the job's output should use (an older one is revoked after two renewals)."""
        return self.handles[-1]

    def _send(self, creds: Mapping) -> int | None:
        for attempt in range(self.retries):
            try:
                code = self.client.renew(self.owner, self.id, creds, self.rekey)
            except httpx.TransportError:
                code = None
            if code is not None and code != 503:
                return code
            if attempt + 1 < self.retries:
                time.sleep(self.retry_delay * (attempt + 1))
        return None

    def step(self) -> None:
        """One renewal, run by the shared thread."""
        with self._busy:
            if self.stopped:
                return
            try:
                h = mint(self.grants, self.lifetime, self.options)
            except Exception as e:  # noqa: BLE001  the credential in use runs out; the job fails by itself
                self.error = f"cannot mint: {type(e).__name__}"
                log.exception("renewal of %s: cannot mint", self.id)
                _scheduler.remove(self)
                return
            code = self._send(h)
            if code != 200:
                h.revoke()
                self.finished, self.lost, self.error = code == 410, code in (None, 404), f"renewal answered {code}"
                _scheduler.remove(self)
                return
            self.handles.append(h)
            if self.on_mint:
                self.on_mint(h)
            while len(self.handles) > 2:  # the one before the last may still be in use by a transfer that began before this renewal
                self.handles.pop(0).revoke()

    def stop(self) -> None:
        _scheduler.remove(self)
        with self._busy:  # waits for a renewal in flight
            self.stopped = True
            while self.handles:
                self.handles.pop().revoke()


def renew_loop(client, grants, job_id: str, owner: str, first=None, **kw) -> RenewLoop:
    """Start renewing the credentials of job `job_id` (from `Client.post`: `id`, `owner`) for `grants`; `first` is the handle the job was posted with."""
    return RenewLoop(client, grants, job_id, owner, first, **kw)
