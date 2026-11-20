# Plan: Processor handoff (worker support for multi-step processors)

> **Status: draft, decided in discussion; open items are under "Open questions".** Written for the article-graph consumer; general to cadmus.

## Problem / Goal

The planned processors are not independent: [media-decode](02-media-decode.md) produces `audio.opus`, keyframes and frame files that [asr](04-asr.md), [caption](05-caption.md) and
[frame-ocr](06-frame-ocr.md) consume; [asr](04-asr.md) and [caption](05-caption.md) split their input into chunk or window *files*, run a primary and a fallback processor on each
and merge the results afterwards. The worker (`server/cadmus_parser_server/worker.py`) as it is gives most of this but not all of it:

| Needed | Today (read from `worker.py`) |
|---|---|
| A later processor reads an earlier one's files for the same input | `Ctx.work` is the job-wide scratch dir, the same for every processor, but `sink.put_file` **copies the file into `out/` and deletes the source**, so a file handed to the sink is gone from scratch at once |
| Run the chain on a synthetic member (a chunk, a window) | `ctx.recurse(path, name)` does it (runs `run_file` with a longer address, depth-limited by `MAX_DEPTH`), also for non-archive members |
| The parent reads what a member's processors produced | `recurse` returns `None`; the member's results go only to the sink |
| Primary processor fails, fallback tries | works: a processor that raises leaves a note and the loop continues; `claim()` stops the later ones |
| Members processed in parallel | no: `recurse` is synchronous and sequential |

Goal: the smallest worker changes that make the four plans above implementable, without changing what existing processors see.

## Design

### 1. Scratch is kept until the job ends (decided)

- **`sink.put_file(rel, src, ...)` no longer deletes `src`.** The file still reaches `out/` for the wrapper to upload (the wrapper has no `DAC_OVERRIDE`; it reads artefacts only
  through `out/`'s group), and the wrapper still uploads it to blob storage as soon as its record arrives and removes the `out/` entry afterwards. What changes is that the
  parser's scratch copy stays. This is the default, there is no opt-in flag.
- **No new handoff API.** The scratch directory is the handoff area: a processor writes files under `ctx.work`, later processors of the same job read them. A processor
  names its files under `ctx.work/<processor name>/<file.rel>/` (helper `ctx.dir()` returns and creates it); reading another processor's directory is the contract, documented
  in each plan (`media-decode` writes `audio.opus`, `keyframes/`, `keyframes.json`, `frames/`, `decode.json`).
- **Nothing is deleted during the job.** The worker removes no scratch file, so unpacked archive members and everything else stay until the end. **At the end of the job all
  scratch is wiped by the wrapper's existing wipe-and-verify**, unchanged.
- **Disk.** What a job may write is bounded by the existing limits (per-file `fsize_mib`, `output_mib`, `input_mib`, per-class); an archive bomb can therefore write up to those
  limits, as before, and no further limit is added. Operators size `PARSE_SCRATCH` for the original plus the kept intermediates of the largest job.
- **No doubling of large files.** `out/` gets the file as a **hard link** to the scratch file (falling back to a copy if linking fails), with the inode's group set to `out/`'s
  group and group-read added (the parser's umask 007 and `out/`'s setgid do not apply to a link). The wrapper's delete after upload then removes only the link.
  `put_bytes` is unchanged (the data is in memory); a processor that wants to hand bytes on writes a file.

### 2. `ctx.recurse` returns a result handle; synthetic members are not indexed (decided)

`ctx.recurse(path, name, synthetic=False)` returns a `Member`. A member made with `synthetic=True` (a chunk, a window: not a real member of an archive) **gets no entry in `index.json`**,
because a consumer reading the index would take it for part of the input. Everything its processors would have reported lands on the nearest real ancestor's entry instead: its notes
are added to that entry's `notes`, and the outputs it announces are listed in that entry's `outputs` (under their own paths, which carry the chunk name, for example
`asr/original/chunk-0003.opus/segments.json`). Real archive members are indexed as today. Synthetic members count against `MAX_DEPTH` but not against `MAX_ARCHIVE_MEMBERS`
(which caps index entries); a separate cap `MAX_SYNTHETIC_MEMBERS` (default 10000) bounds them. The returned `Member` has `.address`, `.claimed_by` (processor name or `None`), `.outputs` (the output paths announced for it, relative) and `.dir(processor)`
(the member's directory under the kept scratch). A parent such as `asr-split` reads each chunk's result from the member's kept files and `.outputs`. `.outputs` comes from the
worker's own record of what the sink announced, not from disk.

### 3. Passing an attempt to the next processor

No mechanism of its own: a failing processor writes its salvaged attempt to its own `ctx.dir()` (`attempt.json`) before returning without a claim; `asr-degrade` reads the attempts
of `asr-primary` and `asr-fallback` from the same member. Documented in `processors.md` as the pattern "primary, fallback, last resort".

### 4. Parallel members

`ctx.recurse_many(items, workers=4)` (`items` = `[(path, name)]`) runs the chain on members in a thread pool and returns the `Member`s in input order. It must keep: the sink and
the `claimed` flags per member (no shared mutable state), one `write` per protocol record (already atomic up to 4096 bytes), and the job's wall-clock deadline and rlimits unchanged
(threads are in the same sandboxed process). `workers` is capped by an operator setting (`PARSE_MEMBER_WORKERS`, default 4). A processor that is not thread-safe declares
`threadsafe = False`; the pool then runs its members serially.

## Open questions

1. **Audit of reliance on the old unlink.** Existing processors (`archives`, `documents`, `markdown`, `metadata`) may use `put_file` to free scratch early (a large derived file). Read each
   and decide whether any needs an explicit delete now; the limits stay as the bound regardless.
2. **Hard link vs copy.** Linking needs `work/` and `out/` on the same filesystem (both are under the job dir today) and a permission change on the shared inode; verify it does not
   let the parser alter a file after the wrapper has begun reading it (the parser can already write the scratch file; a link means it can also write the `out/` file mid-upload). If
   that is a problem, copy and accept the disk cost for the large files, or have the wrapper size-check and hash after upload.
3. Thread safety of the existing `Sink` implementations and of `ctx.claim()` under `recurse_many`; verify by reading `worker.py` and add locking where needed.
4. *(Decided.)* Chunk and window processors write no outputs; only the parent writes the merged `transcript.json` / `captions.json`, which carry the per-chunk outcomes. A synthetic
   member's outputs and notes are still attached to its real ancestor, as a general rule, but the planned processors only use it for notes.

## Implementation order

1. `put_file` keeps the source (hard link with the group fix, copy fallback); audit (open question 1); tests that a kept file stays readable by a later processor, is uploaded once, and is wiped at job end.
2. `ctx.dir()`; `Member`, the `recurse` return value and `synthetic=True` (no index entry, outputs and notes attached to the real ancestor, `MAX_SYNTHETIC_MEMBERS`); tests with a fake chain, including that the index lists no chunk entries.
3. `recurse_many` with `threadsafe`, locking, limits; tests (order, one failing member, deadline).
4. Docs. Then [media-decode](02-media-decode.md) and the model plans build on it.

## Files to change

| File | Change |
|---|---|
| `server/cadmus_parser_server/worker.py` | `put_file` keeps its source, `ctx.dir()`, `Member`, `recurse` result, `recurse_many` |
| `server/cadmus_parser_server/limits.py` / `wrapper.py` | `PARSE_MEMBER_WORKERS` |
| `server/tests/` | the tests above |

## Docs to update

`docs/processors.md` (scratch kept until job end, `ctx.dir()`, members, the primary/fallback/last-resort pattern, `threadsafe`), `docs/reference.md` (`PARSE_MEMBER_WORKERS`),
`docs/security.md` (what the parser can touch in `out/`), `docs/parser-image.md` (`PARSE_SCRATCH` sizing).
