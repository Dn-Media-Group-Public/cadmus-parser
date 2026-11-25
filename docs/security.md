# Security guide

## What the wrapper guarantees

The **parser** (the process that reads untrusted bytes) is confined to **one reading prefix and one writing prefix**:

- it holds no credential, no token (except one sidecar profile's, if the job asked for it), no store URL and none of the wrapper's environment (only the operator's
  processor settings from `PARSE_PASS_ENV`);
- `put_file` hard-links the scratch file into `out/` (copy if linking fails): the parser can still write that inode until the wrapper has uploaded it, as it could write `out/` files before
  See also: [Plan: processor handoff](implemented-plans/01-processor-handoff.md)
- it runs as a pool uid with no capabilities (`no_new_privs`, ambient capabilities dropped), a private scratch directory it cannot escape, rlimits and a deadline
  per media class, on internal-only networks: it has no route out;
- what it announces is validated: paths relative, no `..`, no symlinks, regular files only (`O_NOFOLLOW`), starting with a top-level directory some installed
  processor declared, so the upload stays under the output base; sizes from the wrapper's own `fstat`; line, record and byte caps; `index.json` and `status.json`
  reserved, so a parser cannot write them;
- after every job the uid's processes are killed and everything it owned is removed and verified; a slot that cannot be cleaned is quarantined, never reused.

## Why usage is trusted

Anything a processor writes into `index.json` is untrusted. `usage` in `status.json` comes from the sidecar (which sees every call) and is written by the wrapper,
neither of which the parser can touch. The parser cannot name the job: it holds a **job token** the wrapper obtained over the sidecar's admin routes
(`ADMIN_TOKEN`, known to the wrapper only), so a header it sends attributes nothing. Job tokens expire.
See also: [Plan: sidecar usage reporting](implemented-plans/03-sidecar-usage-reporting.md)

## What the client must do

The client is **trusted**: it chooses the two base URLs and the credentials, and the wrapper uses them as given. There is no base-URL allow-list, no scheme
allow-list and no option filter. Therefore:

1. **Mint job-scoped credentials.** Read the input prefix, write the output prefix, nothing else (`read` = read and list, `write` = read, list and write). The
   wrapper cannot tell a job's prefix from any other prefix; only the store's own enforcement of the credential confines it. A credential broader than the job
   (a project-wide read, say) lets a client bug make the wrapper copy or overwrite whatever the credential reaches. Choose a wider read grant only deliberately.
2. **Never build URLs or options from untrusted input.** They go to fsspec as given: `file://`, `http://`, chained filesystems (`simplecache::s3://...`) and, for
   `gcsfs`, a local credentials file are all reachable. Take them from your own validated state (ids you generated, not paths a user typed).
3. **Keep output bases disjoint.** A job's identity is its output base URL. The wrapper refuses (409) only an **identical** base that is running; nested or
   overlapping bases are not detected and share (and can corrupt) each other's `status.json`, `index.json` and artefacts.
4. **Keep the wrapper token away from untrusted data.** It is one shared secret, in your services' environment. Only your services, on their per-client parse
   networks, can reach the wrapper at all (the overlay generator does that wiring).
5. **Authorise your end users yourself.** The wrapper authorises only the client, by its token.

## What is accepted

A stolen wrapper token, with no store credential, makes the wrapper an authenticated fetcher and uploader: it can be told to pull from an attacker's bucket on any
host the egress proxy allows (the whole public internet; private ranges, the metadata server and other stack services are blocked, except what
`PARSE_PROXY_ALLOW` names), run the files through the parsers (free compute, and a way to attack the parsers from outside the intended path) and upload the
results elsewhere. With no scheme filter it can also name `file://` or `http://` URLs and make the wrapper read its own files or make requests; the wrapper's
secrets are readable only by its own uid, but treat the token as the key to that. It cannot reach your real store without a credential for it. A client bug, or a
stolen token together with a credential, reaches whatever that credential reaches. Rotate `WRAPPER_TOKEN` and `PROXY_TOKEN` if either may have leaked; keep the two
different.

Resource abuse is bounded by the operator's limits only (`PARSE_LIMITS`): every built-in is finite. A caller cannot raise a limit.

## Sidecars

A token selects a profile, and a profile is only what the job was given: a job can use no other profile. Sidecars hold the real endpoint credentials and have no
store access; they pin their own egress. Keep tokens unique and different from `WRAPPER_TOKEN` and `PROXY_TOKEN`. The `layout-sidecar` is one more of these (own internal network `parse-layout`, own pinned egress; a `yolo-onnx` profile
calls nobody, its weights are baked into the image at build time from a pinned URL and checksum and nothing is downloaded at runtime); a job holds only the tokens of the layout profiles in its chain. See also: [Plan: Figure extraction](implemented-plans/figure-extraction.md)

## libmagic

File type detection (`filetype.detect`) runs libmagic on untrusted bytes inside the sandboxed processor process, like exiftool and ffmpeg. See also: [Plan: file type detection](../implemented-plans/07-filetype-detection.md)
