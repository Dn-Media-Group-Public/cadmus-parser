"""Keep a long job's credentials alive: the client pushes. A thread mints fresh credentials every `interval` seconds (well under half the
lifetime) and sends them to the replica that owns the job. Stopping the pushes ends the job's access when the current credential expires."""

import logging
import threading
import time
from collections.abc import Mapping

import httpx

from cadmus_parser.minting import mint

log = logging.getLogger("cadmus_parser.renew")


class RenewLoop:
    """`stop()` ends the thread and revokes every credential the loop minted and `first`. `lost` is True when the job's replica is gone (404 or
    unreachable), `finished` when it answered 410; the client then marks the job failed ("wrapper lost") or lets the status say."""

    def __init__(self, client, grants, job_id: str, owner: str, first=None, interval: float = 300, lifetime: int = 900, options: dict | None = None,
                 retries: int = 3, retry_delay: float = 1, rekey: Mapping | None = None, on_mint=None):
        """`rekey` = {grant url: posted url} when a grant was wider than a posted url (see Client.renew). `on_mint(handle)` is called with every
        handle the loop mints, for a client that keeps a registry of live credentials."""
        self.client, self.grants, self.id, self.owner, self.rekey, self.on_mint = client, list(grants), job_id, owner, rekey, on_mint
        self.interval, self.lifetime, self.options, self.retries, self.retry_delay = interval, lifetime, options, retries, retry_delay
        self.handles = [first] if first is not None else []
        self.lost = self.finished = False
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"renew-{job_id[:6]}")
        self._thread.start()

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
            if self._stop.wait(self.retry_delay * (attempt + 1)):
                return None
        return None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                h = mint(self.grants, self.lifetime, self.options)
            except Exception as e:  # noqa: BLE001  the credential in use runs out; the job fails by itself
                self.error = f"cannot mint: {type(e).__name__}"
                log.exception("renewal of %s: cannot mint", self.id)
                return
            code = self._send(h)
            if code != 200:
                h.revoke()
                self.finished, self.lost, self.error = code == 410, code in (None, 404), f"renewal answered {code}"
                return
            self.handles.append(h)
            if self.on_mint:
                self.on_mint(h)
            while len(self.handles) > 2:  # the one before the last may still be in use by a transfer that began before this renewal
                self.handles.pop(0).revoke()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=60)
        while self.handles:
            self.handles.pop().revoke()


def renew_loop(client, grants, job_id: str, owner: str, first=None, **kw) -> RenewLoop:
    """Start renewing the credentials of job `job_id` (from `Client.post`: `id`, `owner`) for `grants`; `first` is the handle the job was posted with."""
    return RenewLoop(client, grants, job_id, owner, first, **kw)
