# Client and credential minting

`pip install ./client` (`cadmus-parser`, import `cadmus_parser`; extras `[mint-s3]` = boto3, `[mint-gcs]` = google-auth). It does not depend on the service.

## The whole job: `Client.run`

```python
from cadmus_parser import Client

client = Client("http://parser:8080", token, store_options={"endpoint_url": "http://store:9000"})   # your own non-secret store options
status, index = client.run("s3://bucket/jobs/42/in", "s3://bucket/jobs/42/out", mint_options=my_store_identity,
                           options={"files": {"base_url": "https://example.org/"}}, features={"ocr": "lan"})
```

It mints credentials limited to the input (read) and output (write) prefixes, posts the job, keeps the credentials renewed while it runs, waits, reads the index and revokes the
credentials. `status` is `{"status": "done" | "failed", "error"?: ...}`; `index` is `{"files": {...}}` (see the [reference](reference.md)). It raises `ParseError` when the wrapper refuses,
and whatever `mint` raises. Options: `grants=` a wider list than the default (a read grant for a whole project: each posted URL uses the grant that contains it), `lifetime`
(900 s), `renew_every` (300 s), `poll_timeout` (12 min: set it above your largest `deadline_s`), `on_mint(handle)` called with every credential minted (for a registry of live ones).
Everything below is what `run` is made of, for when you need the pieces.

## Posting a job and following it (the pieces)

```python
from cadmus_parser import Client, ParseError

client = Client("http://parser:8080", token, store_options={"endpoint_url": "http://store:9000"})  # your own non-secret options, merged under credentials when reading
started = client.post("s3://bucket/jobs/42/in", "s3://bucket/jobs/42/out", creds,   # creds: {url: storage_options}, as mint returns
                      options={"files": {"base_url": "https://example.org/"}}, features={"ocr": "lan"})
status = client.wait("s3://bucket/jobs/42/out", creds["s3://bucket/jobs/42/out"], poll_timeout=12 * 60)
index = client.index("s3://bucket/jobs/42/out", creds["s3://bucket/jobs/42/out"])      # {"files": {...}} (see the reference)
```

- `post` raises `ParseError(status, message)` (status 0 = no answer); one retry after a second on 503 or a refused connection. `started.id` and `started.owner`
  are what renewals need.
- There is no GET: `status`, `wait` and `index` read `status.json` / `index.json` from the output base with the credential you already have. `status` applies the
  heartbeat rule (`running` older than 30 s reads as `failed`, "wrapper lost"); `wait` polls with backoff (50 ms to 1 s) and returns `{"status": "done" | "failed",
  "error"?}`; a `poll_timeout` that is too short for the longest class reads as failed. The `options` of `read`, `status`, `wait` and `index` may be a callable
  returning the current options (use it with a renewed credential).
- `input_options=` / `output_options=` replace the lookup in `creds` when a grant is wider than a posted URL (a read grant for a whole project, a posted input of one job).
  Nothing is filtered: build URLs and options from your own state, never from untrusted input.

## Minting job-scoped credentials

```python
from cadmus_parser import mint, sweep

grants = [("s3://bucket/jobs/42/in", "read"), ("s3://bucket/jobs/42/out", "write")]
with mint(grants, lifetime=900, options=my_store_identity) as creds:   # revoked at the end of the block
    ...
```

- `grants` = `[(url, "read" | "write")]`. **`read` means read and list; `write` means read, list and write** (the wrapper reads `status.json` under the output base
  and lists the input base). The credential allows exactly those prefixes and nothing else; the wrapper cannot verify that, so it is your job to grant no more
  than the job needs (the [security guide](security.md)).
- It returns a `Handle`: the mapping `{url: storage_options}`, one entry per grant, to look up each side by its own URL. One credential (one role, policy and
  session) is minted **per store**; grants in the same store share it and their entries hold the same credential.
- S3 / SeaweedFS: a role and policy per store plus STS (works on AWS and SeaweedFS; needs IAM permissions and a `sweep`). GCS: a Credential Access Boundary
  (downscoped token); it cannot be revoked and lives as long as the source credential. Other schemes raise. `options` is your own identity for the store
  (fsspec-style: `key`, `secret`, `token`, `endpoint_url`, `client_kwargs`); without it boto3 / google-auth find their defaults. Every failure raises; there is
  never a fallback to your own credential.
- **Revoking.** `handle.revoke()` removes the S3 role at once (idempotent). `handle.to_dict()` is a plain structure (role identifiers, store, creation time,
  **no secret**) you can keep in a database; `Handle.from_dict(d, options)` restores it in another process to `revoke()` (a restored handle has no credentials:
  those exist only in the process that minted them). The context manager revokes on exit.
- **Sweeping.** `sweep(max_age=1200, options=...)` deletes helper-created roles (named `cadmus-parser-<random>`; it touches no others) older than `max_age`
  seconds: those a crashed client never revoked. Schedule it from a periodic task of your own.

## Keeping credentials alive: `renew_loop`

A long job (a video, a large archive) outlives any credential lifetime. The **client pushes** renewals; the wrapper never connects to the client.

```python
from cadmus_parser import renew_loop

loop = renew_loop(client, grants, started.id, started.owner, first=creds, interval=300, lifetime=900, options=my_store_identity)
try:
    status = client.wait(out_url, lambda: loop.current[out_url])
    index = client.index(out_url, loop.current[out_url])
finally:
    loop.stop()      # stops the pushes, revokes everything the loop minted and `first`
```

- **One shared thread serves every running job** (it starts with the first `renew_loop` and ends with the last `stop()`); a renewal blocks it for the length of its HTTP call.
- Every `interval` seconds (well under half the lifetime) it mints new credentials and sends them with `POST /jobs/renew` to the `owner` address from the 202.
  A retry on 503 or a transient connection error is part of the loop. After a successful renewal the credential **before the previous one** is revoked (a transfer
  that began before the renewal may still be using the previous one), so roles do not pile up.
- It stops on any non-200 answer: `loop.finished` (410) means the job is over; `loop.lost` (404, or an unreachable `owner` after the retries) means the replica and
  the job are gone: mark the job failed, "wrapper lost". `loop.error` says why. A client restart kills its loops; the job then fails when its credential
  expires (resuming renewal across a restart is out of scope).
- `rekey={grant_url: posted_url}` renames the keys of what is sent when a grant is wider than a posted URL (the wrapper matches keys against the URLs the job was
  posted with); `on_mint(handle)` is called with every handle the loop makes, for a client that keeps a registry of live credentials.
