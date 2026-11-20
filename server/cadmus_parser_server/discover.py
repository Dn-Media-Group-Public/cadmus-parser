"""`python -I -m cadmus_parser_server.discover`: the names and top_dirs of the installed processors as JSON. The wrapper runs this once at start,
as its own uid in a separate process, so that it imports no processor code and still knows (independently of any parser) which top-level
directories an output path may start with."""

import json

from cadmus_parser_server import processors

if __name__ == "__main__":
    ps = processors.load()
    print(json.dumps({"processors": sorted(p.name for p in ps), "top_dirs": sorted({d for p in ps for d in p.top_dirs})}))
