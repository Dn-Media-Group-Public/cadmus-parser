"""Usage reporting for sidecars (docs/sidecar-protocol.md, "Usage reporting"). The wrapper registers a job over the admin routes (a secret the parser never
holds) and gets a job token, which it hands the parser in place of the profile token. Every call carries it, so the sidecar attributes the call to the job
without trusting a header. The wrapper reads the records when the job ends. Everything is in memory; job tokens expire, so a crashed wrapper leaks nothing."""

import hmac
import secrets
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


class Usage:
    def __init__(self, admin_token: str | None, max_calls: int = 20000, clock=time.time):
        self.admin, self.max_calls, self.clock = admin_token, max_calls, clock
        self.tokens: dict = {}  # job token -> (job id, profile name, expiry)
        self.jobs: dict = {}  # job id -> {"calls": [...], "truncated": bool, "expiry": float}

    def _purge(self) -> None:
        now = self.clock()
        self.tokens = {t: v for t, v in self.tokens.items() if v[2] > now}
        self.jobs = {j: v for j, v in self.jobs.items() if v["expiry"] > now}

    def resolve(self, token: str) -> tuple[str, str] | None:
        """(profile name, job id) of a live job token. Constant time over the table, like the profile lookup."""
        self._purge()
        found = None
        for t, (job, profile, _) in self.tokens.items():
            if hmac.compare_digest(token.encode(), t.encode()):
                found = (profile, job)
        return found

    def record(self, job: str | None, **call) -> None:
        """Append one upstream attempt; `job` None (a profile-token call) records nothing."""
        rec = self.jobs.get(job)
        if rec is None:
            return
        if len(rec["calls"]) >= self.max_calls:  # keep the first ones
            rec["truncated"] = True
        else:
            rec["calls"].append({**call, "t": self.clock()})

    def _authed(self, request: Request) -> bool:
        h = request.headers.get("authorization", "")
        return bool(self.admin) and hmac.compare_digest(h[7:].encode() if h.startswith("Bearer ") else b"", self.admin.encode())

    def routes(self, profiles: dict) -> list:
        async def register(request: Request):
            if not self._authed(request):
                return JSONResponse({"error": "unauthorized"}, 401)
            try:
                body = await request.json()
                job, profile, ttl = str(body["job"]), body["profile"], float(body.get("ttl") or 3600)
            except Exception:  # noqa: BLE001
                return JSONResponse({"error": "bad request"}, 400)
            if profile not in profiles:
                return JSONResponse({"error": "unknown profile"}, 404)
            self._purge()
            token, expiry = secrets.token_urlsafe(32), self.clock() + ttl
            self.tokens[token] = (job, profile, expiry)
            self.jobs[job] = {"calls": [], "truncated": False, "expiry": expiry}
            return JSONResponse({"token": token})

        async def collect(request: Request):
            if not self._authed(request):
                return JSONResponse({"error": "unauthorized"}, 401)
            self._purge()
            job = request.path_params["job"]
            rec = self.jobs.pop(job, None)
            self.tokens = {t: v for t, v in self.tokens.items() if v[0] != job}
            if request.method == "DELETE":
                return JSONResponse({"ok": True})
            if rec is None:
                return JSONResponse({"error": "unknown job"}, 404)
            return JSONResponse({"calls": rec["calls"], "truncated": rec["truncated"]})

        return [Route("/admin/jobs", register, methods=["POST"]), Route("/admin/jobs/{job}/usage", collect, methods=["GET"]),
                Route("/admin/jobs/{job}", collect, methods=["DELETE"])]
