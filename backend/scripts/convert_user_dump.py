#!/usr/bin/env python3
"""Convert a format-1 `dump_user.py` file (one JSON document) to format 2
(JSON Lines: header line, then one node per line) so `load_user.py` can
stream it inside a memory-capped container. Run anywhere with enough RAM
for the format-1 file (a workstation); stdlib only.

    python backend/scripts/convert_user_dump.py data/rich.json data/rich.jsonl
"""
import json
import sys


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as f:
        dump = json.load(f)
    if dump.get("format") != 1:
        sys.exit(f"expected a format-1 dump, got {dump.get('format')!r}")
    nodes = dump.pop("nodes")
    dump["format"] = 2
    with open(dst, "w", encoding="utf-8") as out:
        out.write(json.dumps(dump, ensure_ascii=False) + "\n")
        for row in nodes:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {dst}: {len(nodes)} nodes, {len(dump.get('profiles', []))} profiles")


if __name__ == "__main__":
    main()
