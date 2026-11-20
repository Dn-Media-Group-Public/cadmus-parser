# Versioning and upgrades

The repository is versioned with tags (`vMAJOR.MINOR.PATCH`); the two packages (`cadmus-parser`, `cadmus-parser-server`) and the images are versioned together.
A consumer pins a tag (a git submodule pointing at it works well).

`GET /version` on the wrapper returns the **API version** (`{"api": "1"}`): the wrapper HTTP API (`POST /jobs`, `POST /jobs/renew`), the `status.json` and
`index.json` shapes, the worker protocol and the sidecar protocol. Breaking changes bump the major version; additions (a new optional field, a new feature, a new
limit name) do not.

## Upgrade notes

- **0.1.0**: first release, extracted from a consumer's own parser service. The wrapper takes two base URLs and per-side credentials instead of a job address; there is
  no GET of a job (read `status.json` / `index.json` from the output base); `index.json` is per input file and recursive; processors are entry points, ordered
  and dispatched per file; limits are per media class (`PARSE_LIMITS`, finite built-ins); renewals are pushed (`POST /jobs/renew`); sidecars speak
  `POST /process`; the ExifTool extension groups of the `files` processor are `Cadmus-<Format>` (formerly named after the first consumer).
