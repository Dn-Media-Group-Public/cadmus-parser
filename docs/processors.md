# How to write a processor

A processor is the code that interprets a file. It runs inside the sandboxed parser process: **no storage access, no network, no credential**. It sees a file on
disk and writes artefacts through a sink; the wrapper moves the bytes.

## Registering

A Python package registers one or more entry points in the group `cadmus_parser.processors` (the group name is the same for every processor, whatever service
loads it):

```toml
[project.entry-points."cadmus_parser.processors"]
upper = "toy.processor:Upper"
```

Each resolves to a processor object (or a class, which is instantiated with no arguments) with:

| Member | Meaning |
|---|---|
| `name` | unique; the key of `options` in the job request |
| `before`, `after` | optional tuples of processor names: `before` = processors that must run after this one, `after` = must run before it. Names that are not installed are ignored; either side may declare an edge |
| `top_dirs` | the top-level directories (below the output base) this processor may write; the wrapper allows only the union over installed processors |
| `accepts(file) -> bool` | cheap; may look at `file.name`, `file.size`, the first bytes |
| `process(sink, file, ctx)` | do the work |

Nothing the caller sends is imported, and the consumer does not select processors: the base image has none, and an image has whichever packages it installs. A
processor whose import fails (an extra that is not installed) is skipped with a line on the parser's stderr.

## Order and dispatch

The worker sorts the processors topologically at the start of every job; free choices are broken alphabetically by name, so the order is the same on every run.
A **cycle fails the job at once** with the processors involved in `status.json`. For each file the worker runs **every processor whose `accepts` is true, in
order**; each adds its own outputs. A processor may claim a file (`ctx.claim()`), which stops the later ones for that file. Two writes of the same output path
in a job are refused: the later is dropped and noted on its input's entry.

A file no processor accepts gets the note "not parsed (no processor)", which is not a failure. A processor that raises leaves a note on that entry and the rest
continue. The parser is still one process per job: a crash or a limit kill ends the whole job.

## The interface

```python
class Upper:
    name = "upper"
    top_dirs = ("upper",)

    def accepts(self, file):
        return file.name.endswith(".txt")

    def process(self, sink, file, ctx):
        sink.info(content_type="text/plain")
        with open(file.path, "rb") as f:
            sink.put_bytes(f"upper/{file.rel}", f.read().upper(), "text/plain", meta={"shouted": True})
```

- `file.path` is the file on disk; `file.name` the last path element; `file.address` the input address as a list, one element per archive level
  (`["a.zip", "docs/m.pdf"]`); `file.rel` that address as one name for naming outputs (`a.zip.d/docs/m.pdf`); `file.depth` 0 for an input, more for an archive
  member; `file.size`.
- `sink.put_bytes(rel, data, content_type, meta=None)` and `sink.put_file(rel, src, content_type, meta=None)` (moves `src`, a file in `ctx.work`, into the
  output) write `rel`, a path below the output base starting with one of your `top_dirs` and at least one more element. `sink.info(content_type=None, meta=None,
  size=None)` says something about the input itself (`size` counts only for archive members); `sink.note(text)` adds a note to its entry. `meta` is a small
  JSON object (at most 2000 bytes) passed through to `index.json`, e.g. `{"text": true}`. All of it is untrusted by the consumer: the parser may be hostile.
- `ctx.options` is this processor's own entry of the job's `options`; `ctx.features` the feature names the job asked for (their `<PREFIX>_URL` /
  `<PREFIX>_TOKEN` are in the environment, for the profile it named); `ctx.work` a scratch directory.
- **Archives.** Unpack into `ctx.work` yourself and call `ctx.recurse(path, name)` for each member: the worker runs the same chain on it (as a `File` with a longer
  address, up to 8 levels) and builds the nested entries. No processor needs archive logic of its own. Respect your own bounds (bytes, members).

## Testing

Drive the worker's `Runner` with an in-memory sink (see `server/tests/conftest.py`: `MemSink`), or run a whole job against `file://` URLs with the wrapper
(`server/tests/harness.py`). Settings the operator tunes reach the parser only through names listed in `PARSE_PASS_ENV`.
