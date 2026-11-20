"""Post a job to the wrapper and follow it. The wrapper has no GET: the client reads `status.json` and `index.json` from the output base with the
credential the consumer already has (the heartbeat rule: the wrapper rewrites `running` every 10 s, one older than 30 s means the wrapper is lost)."""

import json
import time
from collections.abc import Mapping
from typing import NamedTuple

import fsspec
import httpx

STALE = 30  # seconds: a `running` status.json not rewritten for this long is a lost wrapper


class ParseError(Exception):
    """The wrapper refused or could not be reached. `status` is the HTTP status (0 = no answer), the message is the wrapper's `error` when it gave one."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}" if status else message)
        self.status, self.message = status, message


class Started(NamedTuple):
    id: str  # opaque parse job id, the handle for renewals
    owner: str  # ip:port of the replica that took the job, on this consumer's network


def wire(options: dict) -> dict:
    """Scoped fsspec options as JSON: a GCS downscoped token is a Credentials object, sent as its access token."""
    tok = options.get("token")
    if tok is not None and not isinstance(tok, str):
        import google.auth.transport.requests
        tok.refresh(google.auth.transport.requests.Request())
        return {**{k: v for k, v in options.items() if k != "token"}, "gcs_access_token": tok.token}
    return options


def _error(r: httpx.Response) -> ParseError:
    try:
        msg = r.json()["error"]
    except Exception:  # noqa: BLE001
        msg = r.text[:200]
    return ParseError(r.status_code, msg)


class Client:
    """`store_options`: this consumer's own non-secret fsspec options for the store (endpoint ...), merged under the credentials when it reads."""

    def __init__(self, url: str, token: str, store_options: dict | None = None, timeout: float = 30):
        self.url, self.token, self.store_options, self.timeout = url.rstrip("/"), token, store_options or {}, timeout

    @property
    def _head(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def post(self, input_url: str, output_url: str, creds: Mapping | None = None, *, options: dict | None = None, features: dict | None = None) -> Started:
        """POST /jobs. `creds` is the `{url: options}` mapping `mint` returns: each side gets the entry of its own url. One retry after a second on
        503 or a refused connection. Raises ParseError."""
        creds = creds or {}
        body = {"input": {"url": input_url, "options": wire(creds.get(input_url, {}))}, "output": {"url": output_url, "options": wire(creds.get(output_url, {}))},
                **({"options": options} if options else {}), **({"features": features} if features else {})}
        for attempt in (0, 1):
            try:
                r = httpx.post(f"{self.url}/jobs", json=body, headers=self._head, timeout=self.timeout)
            except httpx.TransportError as e:
                if attempt:
                    raise ParseError(0, f"parser unavailable: {type(e).__name__}") from None
                time.sleep(1)
                continue
            if r.status_code == 503 and not attempt:
                time.sleep(1)
                continue
            break
        if r.status_code != 202:
            raise _error(r)
        j = r.json()
        return Started(j["id"], j["owner"])

    def renew(self, owner: str, job_id: str, creds: Mapping) -> int:
        """POST /jobs/renew to the replica that owns the job: 200 renewed, 404 unknown (the replica is gone), 410 finished. Returns the status code;
        raises httpx.TransportError when the replica cannot be reached."""
        options = {url: wire(o) for url, o in creds.items()}
        return httpx.post(f"http://{owner}/jobs/renew", json={"id": job_id, "options": options}, headers=self._head, timeout=self.timeout).status_code

    def read(self, base_url: str, name: str, options: dict | None = None) -> bytes:
        fs, base = fsspec.core.url_to_fs(base_url, use_listings_cache=False, **{**self.store_options, **(options or {}), "skip_instance_cache": True})
        return fs.cat_file(f"{base.rstrip('/')}/{name}")

    def status(self, output_url: str, options: dict | None = None) -> dict:
        """status.json with the heartbeat rule applied: {"status": "not_started" | "running" | "done" | "failed", "error"?: ...}."""
        try:
            s = json.loads(self.read(output_url, "status.json", options))
        except FileNotFoundError:
            return {"status": "not_started"}
        if s["status"] == "running" and time.time() - s["updated"] > STALE:
            return {"status": "failed", "error": "wrapper lost"}
        return s

    def wait(self, output_url: str, options: dict | None = None, poll_timeout: float = 12 * 60) -> dict:
        """Poll until `done` or `failed` (backoff 50 ms to 1 s). The poll window is a parameter: a long class needs a longer one."""
        delay, deadline = 0.05, time.monotonic() + poll_timeout
        while time.monotonic() < deadline:
            time.sleep(delay)
            delay = min(delay * 1.5, 1.0)
            try:
                s = self.status(output_url, options)
            except (OSError, ValueError):  # a store hiccup; the deadline bounds it
                continue
            if s["status"] in ("done", "failed"):
                return s
        return {"status": "failed", "error": "timed out waiting for the parser"}

    def index(self, output_url: str, options: dict | None = None) -> dict:
        """index.json ({"files": {...}, "notes"?}); empty when there is none (a job that never got far enough)."""
        try:
            return json.loads(self.read(output_url, "index.json", options))
        except FileNotFoundError:
            return {"files": {}}
