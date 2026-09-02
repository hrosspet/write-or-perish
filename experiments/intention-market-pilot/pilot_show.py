"""Print aggregated (or raw) intention lists compactly for a qualitative read.
Private output — never paste into the public research doc."""
import argparse
import textwrap

from pilot_common import AGG_DIR, LUNA, RAW_DIR, accounts, jload, parse_blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=16)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--model", default=LUNA)
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--width", type=int, default=110)
    ap.add_argument("--full", action="store_true", help="print the whole block text")
    args = ap.parse_args()
    for r in accounts()[args.start:args.start + args.count]:
        h = r["username"]
        if args.raw:
            chunks = sorted([jload(p) for p in RAW_DIR.glob(f"{h}_c*.json")],
                            key=lambda c: c["chunk"])
            print(f"\n=== {h} — {len(chunks)} raw chunks ===")
            for c in chunks:
                print(f"--- chunk {c['chunk']} {c['period']} {c['tweets']} tweets ---")
                for b in parse_blocks(c["content"]):
                    print(f"  [{b['group'][:1]}] {b['title']}  ({b['status']})")
            continue
        a = jload(AGG_DIR / f"{h}.{args.model}.json")
        if not a:
            print(f"\n=== {h}: no aggregation for {args.model} ===")
            continue
        print(f"\n=== {h} — {a['n_chunks']} slices, raw {a['raw_count']} → agg {a['agg_count']} "
              f"(E{a['endorsed']}/I{a['inferred']}) ===")
        for b in a["blocks"]:
            if args.full:
                print(textwrap.indent(b["text"], "  "))
                print()
            else:
                body = b["body"].replace("\n", " ")
                print(f"  {b['id']:<22} [{b['group'][:1]}] {b['title']}  ({b['status']})")
                print(textwrap.fill(body, width=args.width, initial_indent="      ",
                                    subsequent_indent="      "))


if __name__ == "__main__":
    main()
