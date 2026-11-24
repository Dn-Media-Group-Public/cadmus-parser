# Versioning and upgrades

The repository is versioned with tags (`vMAJOR.MINOR.PATCH`); the two packages (`cadmus-parser`, `cadmus-parser-server`) and the images are versioned together.
A client pins a tag (a git submodule pointing at it works well).

`GET /version` on the wrapper returns the **API version** (`{"api": "1"}`): the wrapper HTTP API (`POST /jobs`, `POST /jobs/renew`), the `status.json` and
`index.json` shapes, the worker protocol and the sidecar protocol. Breaking changes bump the major version; additions (a new optional field, a new feature, a new
limit name) do not.

## Upgrade notes

- **0.1.0**: first release, extracted from a client's own parser service. The wrapper takes two base URLs and per-side credentials instead of a job address; there is
  no GET of a job (read `status.json` / `index.json` from the output base); `index.json` is per input file and recursive; processors are entry points, ordered
  and dispatched per file; limits are per media class (`PARSE_LIMITS`, finite built-ins); renewals are pushed (`POST /jobs/renew`); sidecars speak
  `POST /process`; the ExifTool extension groups of the `files` processor are `Cadmus-<Format>` (formerly named after the first client).
- **next minor**: new optional profile mode `auth: oauth2` in the ocr/asr/caption sidecars (nothing existing changes); their images also copy `common/upstream_auth.py`. See also: [Plan: OAuth2 upstream auth](implemented-plans/oauth2-upstream-auth.md)
- **next minor**: new operator variables `MAX_PARSE_BYTES_FILES`, `MAX_PARSE_BYTES_IMAGE`, `MAX_PARSE_BYTES_VIDEO`, `MAX_PARSE_BYTES_SOUND`; `MAX_PARSE_BYTES` is kept as their fallback. Behaviour change: `MAX_PARSE_BYTES` now also caps image, video and sound (default 2 GiB each). See also: [Plan: per-processor size caps](implemented-plans/parse-bytes-per-processor.md)
