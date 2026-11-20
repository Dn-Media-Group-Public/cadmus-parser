# Plan: ASR (transcription)

> **Status: draft.** Depends on [processor-handoff](processor-handoff.md), [media-decode](media-decode.md) and [sidecar-usage-reporting](sidecar-usage-reporting.md).

## Problem / Goal

Cadmus cannot turn speech into text. Transcribing long, possibly multi-speaker recordings needs chunking, validation of each model answer, a fallback model,
merging of the chunks and speaker labels that stay consistent across them. The parser must not hold the model endpoint's key.

Goal: an `asr-sidecar` (one call to one model, holds the credentials) and processors that split the audio into chunk **files**, transcribe each chunk with a
primary and a fallback **processor** (cadmus's own priority order), and merge the result into `asr/<file.rel>/transcript.json`.

## Design

### Pieces

- **`asr-sidecar/`** (new, modelled on `ocr-sidecar`): `POST /process` takes one audio chunk and returns the raw segment list from one upstream call (offset, speaker,
  text) plus the upstream finish/block status. It does **no retry, validation or fallback**; those are processors. Two profiles are normally configured, a primary
  and a fallback (a different model, possibly a different endpoint and key), each with its own token. Backends: `transcriptions` (default:
  `/v1/audio/transcriptions`, `verbose_json`/`diarized_json`) or `chat` (chat completions with the chunk attached and a JSON-schema response of segments with `start`
  as `MM:SS`, `speaker`, `text`; diarization by the prompt). `chat` exists because some languages (Norwegian) have no diarization model while an audio-capable
  LLM handles them. For `chat` the output is capped (`max_output_tokens`, default 32768), well below the model maximum, so a runaway repetition ends as a truncation.
  Options as `X-` headers: `X-Chunk-Seconds`, `X-Language`, `X-Context`.
- **Features.** The wrapper's `FEATURES` gains `asr` (`ASR_PROFILES`) and `asr-fallback` (`ASR_FALLBACK_PROFILES`); a job asks for
  `features: {"asr": "<profile>", "asr-fallback": "<profile>"}`; both may point at the same sidecar service.

### Processors (cadmus's priority order does the fallback)

All have `top_dirs = ("asr",)`; the worker runs them in the topological order given by `before`/`after`.

1. **`asr-split`** (after `media-decode`): accepts a file that has `audio.opus` from `media-decode`, only when the job asked for the `asr` feature. Cuts `audio.opus`
   into chunk files in `ctx.work` (`chunk_seconds` default 300, 0 = whole file; `chunk_overlap_seconds` default 30; ffmpeg stream copy at offsets from the probed
   duration; a tail shorter than 60 s merges into the previous chunk) and calls **`ctx.recurse(..., synthetic=True)` on each chunk file** (chunks are not listed in `index.json`; their outputs attach to the recording's entry) (addresses like
   `["original", "chunk-0003.opus"]`). After the last member it merges (below) and writes `transcript.json`.
2. **`asr-primary`** (accepts only chunk files made by `asr-split`): one `POST /process` to the `asr` profile, then validation (below). On success it keeps the
   chunk result in its `ctx.dir()` (`segments.json`, a scratch file, **not an output**) and **claims** the chunk. On `invalid` it retries once on the same sidecar profile (not on `blocked`: blocks are
   deterministic for the same audio). On failure it claims nothing and leaves its best salvaged attempt in its `ctx.dir()`.
3. **`asr-fallback`** (after `asr-primary`): runs only for chunks `asr-primary` did not claim; same call and validation against the `asr-fallback` profile (up to 2 attempts, none
   after `blocked`). Success: keeps the result in its `ctx.dir()`, claims.
4. **`asr-degrade`** (after `asr-fallback`): for a chunk still unclaimed, keeps the salvaged attempt with the most segments, flagged `degraded` (or a result with
   `outcome: "blocked"` and no segments if every attempt was blocked) and claims. **Chunk processors write no outputs: the only output of the feature is the merged `transcript.json`**
   (its `chunks` list holds each chunk's outcome, role, model and attempts), written by `asr-split`.

Each chunk is therefore transcribed independently, and only a failed chunk reaches the fallback; this is the reason for chunks as files.

### Validation (in the processors, shared module `modelcheck.py`)

First failure decides, per attempt:
1. safety block (block reason or blocked finish reason) → `blocked`;
2. segments decoded one JSON element at a time so a truncated answer keeps its complete leading segments; an element failing the schema is skipped; nothing parseable is invalid;
3. a non-normal finish reason (truncation) is invalid, even if segments were salvaged;
4. quality: at least one segment; timestamps never go backwards; the last timestamp within `max(120 s, 20 % of the chunk)` of the chunk end (a larger gap that still
   passes is noted, so the tolerance can be tuned); no repetition loop (the same 8-word sequence with at least 3 distinct words 4 or more times, or the same word 11 or more times in a row).

### Merge (in `asr-split`, deterministic, no model)

- De-duplicate the overlap (keep chunk k up to the overlap midpoint). **Speaker mapping** across chunks by fuzzy-matching the overlapping text (stdlib `difflib`,
  one-to-one, weighted vote); a label with no match becomes a new speaker; the canonical label is the one from the first chunk where the speaker appears. When the
  overlap holds no matchable speech, speakers fall back to new ones and `speaker_scope: "partial"` is recorded.
- **Timestamps.** For `chat` the prompt states the chunk length as the maximum timestamp and asks to split any speaker turn longer than about 45 s. A last timestamp past
  the chunk end rescales the chunk to fit, keeping relative spacing. A compressed timeline is not corrected (it looks like a dropped tail).
- **Context and language.** Option `context` (names, terminology, topics) goes to the prompt (`chat`) or the `prompt` parameter (`transcriptions`; such models imitate the
  style of the text, so write it as transcript-style text). `language` unset = the model detects it.
- The job fails (the parent processor raises) only if every chunk has no text; `blocked` and `invalid` are distinct reasons in the note.

### Output

`asr/<file.rel>/transcript.json`:

```json
{"language": "...", "speaker_scope": "full|partial", "duration": 3612.4,
 "chunks": [{"start": 0, "end": 300, "outcome": "ok|degraded|blocked", "role": "primary|fallback", "model": "...", "attempts": 1}],
 "segments": [{"offset": 12.4, "speaker": "Person 1", "text": "...", "chunk": 0}]}
```

This is the processor's own record: advisory (untrusted). Trusted cost data is the sidecar's usage record in `status.json`.

**Known limitations** (to document): compressed timelines go undetected; retrying a blocked chunk on the same model never helps (only the fallback does); a `transcriptions`
backend without diarization yields no speaker labels; speaker mapping across chunks depends on speech in the overlap.

## Open questions

1. The worker mechanics this plan relies on, all specified in [processor-handoff](processor-handoff.md): the `Member` result of `ctx.recurse` (the merge reads each chunk's
   result from it), kept scratch files (`ctx.dir()`) for a salvaged attempt passed from `asr-primary`/`asr-fallback` to `asr-degrade`, and `ctx.recurse_many` for parallel chunk calls
   (24 chunks for a 2 h recording).
2. A chunk file is an archive-member-shaped address: the 8-level and `MAX_ARCHIVE_MEMBERS` limits apply, which is fine for audio (hundreds of chunks).

## Implementation order

1. [processor-handoff](processor-handoff.md) implemented first.
2. `modelcheck.py` (validation helpers; tests on synthetic responses: truncation, repetition loop, backwards timestamps, block, empty).
3. `asr-sidecar/` (profiles, both protocols, usage), tests with a fake upstream.
4. Processors `asr-split` (cutting, merge, speaker mapping, rescale as pure functions with tests on synthetic chunk outputs), `asr-primary`, `asr-fallback`, `asr-degrade`.
5. Wrapper `FEATURES`, overlay (`--asr`), stack file service; docs.

## Files to change

| File | Change |
|---|---|
| `asr-sidecar/` (new: `app.py`, `Dockerfile`, `start.py`, tests) | sidecar |
| `server/cadmus_parser_server/asr.py` (new), `modelcheck.py` (new, shared with [caption](caption.md)) | processors, validation |
| `server/cadmus_parser_server/wrapper.py` | `FEATURES["asr"] = "ASR"`, `FEATURES["asr-fallback"] = "ASR_FALLBACK"` |
| `server/cadmus_parser_server/overlay.py`, `docker-compose.parse.yml` | `--asr`, service, network |
| `server/pyproject.toml` | four entry points |

## Docs to update

`README.md`, `docs/reference.md` (features, env vars, `transcript.json`), `docs/sidecar-protocol.md`, `docs/processors.md` (the primary/fallback-by-priority pattern with chunk members), `docs/quickstart.md`.
