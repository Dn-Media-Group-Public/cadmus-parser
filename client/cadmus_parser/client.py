"""Post a job to the wrapper and follow it. The wrapper has no GET: the client reads `status.json` and `index.json` from the output base with the
credential the client already has (the heartbeat rule: the wrapper rewrites `running` every 10 s, one older than 30 s means the wrapper is lost)."""

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
    owner: str  # ip:port of the replica that took the job, on this client's network


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
    """`store_options`: this client's own non-secret fsspec options for the store (endpoint ...), merged under the credentials when it reads."""

    def __init__(self, url: str, token: str, store_options: dict | None = None, timeout: float = 30):
        self.url, self.token, self.store_options, self.timeout = url.rstrip("/"), token, store_options or {}, timeout

    @property
    def _head(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def post(self, input_url: str, output_url: str, creds: Mapping | None = None, *, input_options: dict | None = None, output_options: dict | None = None,
             options: dict | None = None, features: dict | None = None) -> Started:
        """POST /jobs. `creds` is the `{url: options}` mapping `mint` returns: each side gets the entry of its own url, unless `input_options` /
        `output_options` say otherwise (a grant wider than the posted url: a project-wide read for one job's input). One retry after a second on 503
        or a refused connection. Raises ParseError."""
        creds = creds or {}
        body = {"input": {"url": input_url, "options": wire(input_options if input_options is not None else creds.get(input_url, {}))},
                "output": {"url": output_url, "options": wire(output_options if output_options is not None else creds.get(output_url, {}))},
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

    def renew(self, owner: str, job_id: str, creds: Mapping, rekey: Mapping | None = None) -> int:
        """POST /jobs/renew to the replica that owns the job: 200 renewed, 404 unknown (the replica is gone), 410 finished. Returns the status code;
        raises httpx.TransportError when the replica cannot be reached. The wrapper matches the keys against the urls the job was posted with:
        `rekey` = {grant url: posted url} renames the keys of `creds` when a grant was wider than a posted url."""
        options = {(rekey or {}).get(url, url): wire(o) for url, o in creds.items()}
        return httpx.post(f"http://{owner}/jobs/renew", json={"id": job_id, "options": options}, headers=self._head, timeout=self.timeout).status_code

    def read(self, base_url: str, name: str, options=None) -> bytes:
        """`options` may be a callable returning the current options (a renewed credential replaces the one the job was posted with)."""
        options = options() if callable(options) else options
        fs, base = fsspec.core.url_to_fs(base_url, use_listings_cache=False, **{**self.store_options, **(options or {}), "skip_instance_cache": True})
        return fs.cat_file(f"{base.rstrip('/')}/{name}")

    def status(self, output_url: str, options=None) -> dict:
        """status.json with the heartbeat rule applied: {"status": "not_started" | "running" | "done" | "failed", "error"?: ...}."""
        try:
            s = json.loads(self.read(output_url, "status.json", options))
        except FileNotFoundError:
            return {"status": "not_started"}
        if s["status"] == "running" and time.time() - s["updated"] > STALE:
            return {"status": "failed", "error": "wrapper lost"}
        return s

    def wait(self, output_url: str, options=None, poll_timeout: float = 12 * 60) -> dict:
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

    def index(self, output_url: str, options=None) -> dict:
        """index.json ({"files": {...}, "notes"?}); empty when there is none (a job that never got far enough)."""
        try:
            return json.loads(self.read(output_url, "index.json", options))
        except FileNotFoundError:
            return {"files": {}}


    def run(self, input_url: str, output_url: str, grants: list | None = None, *, mint_options: dict | None = None, options: dict | None = None,
            features: dict | None = None, lifetime: int = 900, renew_every: float = 300, poll_timeout: float = 12 * 60, on_mint=None) -> tuple[dict, dict]:
        """The whole job: mint credentials limited to the input (read) and output (write) prefixes, post, keep the credentials renewed while it runs, wait,
        read the index, revoke. Returns `(status, index)`: `status` is `{"status": "done" | "failed", "error"?: ...}`; raises ParseError when the wrapper
        refuses and whatever `mint` raises. `mint_options` is your own identity for the store (see `mint`). A wider `grants` list (a read grant for
        the whole project, say) replaces the default; each posted URL then uses the grant that contains it. `on_mint(handle)` sees every credential minted."""
        from cadmus_parser.minting import mint
        from cadmus_parser.renew import renew_loop
        grants = grants or [(input_url, "read"), (output_url, "write")]
        h = mint(grants, lifetime, mint_options)
        if on_mint:
            on_mint(h)
        loop = None
        try:
            gi, go = _grant_for(h, input_url), _grant_for(h, output_url)
            started = self.post(input_url, output_url, input_options=h[gi], output_options=h[go], options=options, features=features)
            rekey = {g: u for g, u in ((gi, input_url), (go, output_url)) if g != u}
            loop = renew_loop(self, grants, started.id, started.owner, h, interval=renew_every, lifetime=lifetime, options=mint_options, rekey=rekey, on_mint=on_mint)
            status = self.wait(output_url, lambda: loop.current[go], poll_timeout)
            return status, self.index(output_url, loop.current[go])
        finally:
            if loop:
                loop.stop()
            h.revoke()


def _grant_for(grants: Mapping, url: str) -> str:
    """The grant URL that covers `url`: the same, else the longest one it lies below."""
    if url in grants:
        return url
    below = [g for g in grants if url.startswith(g.rstrip("/") + "/")]
    if not below:
        raise ValueError(f"no grant covers {url}")
    return max(below, key=len)
