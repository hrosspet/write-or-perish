"""Union of several match runs over the SAME input lists: distinct pairs found
across runs, votes per pair, the accumulation curve, and union degrees.
Also usable across models (same lists, different judge)."""
import argparse
import collections

from pilot_common import MATCH_DIR, jload


def key(d, pid):
    it = d["idmap"][str(pid)]
    return f"{it['handle']}:{it['title']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", required=True,
                    help="match-file glob without .json, e.g. 'default_agg-gpt-5.6-luna_N16_*'")
    args = ap.parse_args()
    runs = [jload(p) for p in sorted(MATCH_DIR.glob(args.glob + ".json"))]
    if not runs:
        print("no runs match", args.glob)
        return
    votes, people, by_intent = collections.Counter(), collections.Counter(), collections.Counter()
    ppairs, seen, cum = set(), set(), []
    for r in runs:
        tag, ps = r["summary"]["tag"], set()
        for m in r["matches"]:
            if not m.get("grounded_ids"):
                continue
            k = tuple(sorted((key(r, m["a"]), key(r, m["b"]))))
            ps.add(k)
            votes[k] += 1
            ppairs.add(tuple(sorted((m["a_handle"], m["b_handle"]))))
        seen |= ps
        cum.append((tag, len(ps), len(seen)))
    for k in votes:
        for side in k:
            by_intent[side] += 1
            people[side.split(":")[0]] += 1
    everyone = {v["handle"] for r in runs for v in r["idmap"].values()}
    n_int = len(runs[0]["idmap"])
    print(f"{len(runs)} runs over {len(everyone)} people / {n_int} intentions → "
          f"{len(votes)} distinct intention pairs, {len(ppairs)} distinct people pairs")
    print("accumulation:")
    for t, n, c in cum:
        print(f"  {t:<60} run={n:>3} cumulative={c:>3}")
    print("votes histogram (votes: pairs):", sorted(collections.Counter(votes.values()).items()))
    print("top intentions by union degree:", by_intent.most_common(6))
    print("people by union degree:", people.most_common())
    print("people never matched:", sorted(everyone - set(people)))
    multi = [(k, v) for k, v in votes.most_common() if v >= 2]
    print(f"\npairs found by >=2 runs ({len(multi)}):")
    for k, v in multi:
        print(f"  {v}x  {k[0][:52]:<52} × {k[1][:52]}")


if __name__ == "__main__":
    main()
