"""Print a match run's pairs with both intentions' text, for the qualitative read.
Private output — never paste into the public research doc."""
import argparse
import textwrap

from pilot_common import MATCH_DIR, jload


def wrap(s, indent="      ", width=110):
    return textwrap.fill(" ".join((s or "").split()), width=width,
                         initial_indent=indent, subsequent_indent=indent)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", help="match file tag, e.g. default_agg_N16_gpt-5.6-luna_run1")
    ap.add_argument("--body", type=int, default=260, help="chars of each intention body to show")
    args = ap.parse_args()
    d = jload(MATCH_DIR / f"{args.tag}.json")
    if not d:
        print("no such run:", MATCH_DIR / f"{args.tag}.json")
        return
    s, idmap = d["summary"], d["idmap"]
    print(f"=== {s['tag']}: {s['matches_grounded']} grounded / {s['matches_total']} total, "
          f"mean conf {s['mean_confidence']}, people with zero: {len(s['people_with_zero'])} ===")
    for i, m in enumerate(d["matches"], 1):
        flag = "" if m.get("grounded_ids") else "  !! UNGROUNDED"
        print(f"\n--- match {i}: {m.get('a')} × {m.get('b')}  [{m.get('kind')}] conf={m.get('confidence')}{flag}")
        for side in ("a", "b"):
            it = idmap.get(str(m.get(side)))
            if it:
                print(f"  {side.upper()} {it['handle']} [{it['group'][:1]}] {it['title']}")
                print(wrap(it["body"][:args.body] + ("…" if len(it["body"]) > args.body else "")))
        print("  WHY:")
        print(wrap(m.get("why", "")))
        print("  NON-OBVIOUS:")
        print(wrap(m.get("non_obvious", "")))


if __name__ == "__main__":
    main()
