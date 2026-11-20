"""The processor registry: every `cadmus_parser.processors` entry point is a processor (name, optional before/after lists of names, top_dirs,
accepts(file), process(sink, file, ctx)). The client never selects one. A processor whose import fails (the `files` extra is not installed)
is skipped and reported on stderr."""

import sys
from importlib.metadata import entry_points

GROUP = "cadmus_parser.processors"


class CycleError(Exception):
    pass


def load(eps=None) -> list:
    out = []
    for ep in entry_points(group=GROUP) if eps is None else eps:
        try:
            obj = ep.load()
        except ImportError as e:
            print(f"processor {ep.name} not loaded: {e}", file=sys.stderr)
            continue
        out.append(obj() if isinstance(obj, type) else obj)
    return out


def order(procs: list) -> list:
    """Topological order of `before`/`after` edges (names not installed are ignored, either side may declare an edge); free choices are
    broken alphabetically, so the order is the same on every run. A cycle raises CycleError naming the processors involved."""
    names = {}
    for p in procs:
        if p.name in names:
            raise CycleError(f"two processors are named {p.name!r}")
        names[p.name] = p
    needs = {n: set() for n in names}  # n -> names that must run before n
    for p in procs:
        for b in getattr(p, "before", ()):
            if b in names:
                needs[b].add(p.name)
        for a in getattr(p, "after", ()):
            if a in names:
                needs[p.name].add(a)
    done, out = set(), []
    while len(out) < len(names):
        ready = sorted(n for n in names if n not in done and needs[n] <= done)
        if not ready:
            raise CycleError("processor cycle: " + ", ".join(sorted(set(names) - done)))
        done.add(ready[0])
        out.append(names[ready[0]])
    return out
