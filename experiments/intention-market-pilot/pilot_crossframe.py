"""People-pair consensus across frames: which pairs of people were matched by
the default-frame runs AND by the prosaic-frame runs (different lists, so the
comparison is at the people level)."""
import argparse
import collections

from pilot_common import OUT, jload


def people_pairs(glob_pattern, frame_dir):
    votes = collections.Counter()
    titles = {}
    for p in sorted((OUT / frame_dir).glob(glob_pattern + ".json")):
        d = jload(p)
        for m in d["matches"]:
            if m.get("grounded_ids"):
                k = tuple(sorted((m["a_handle"], m["b_handle"])))
                votes[k] += 1
                titles.setdefault(k, []).append(f"{m['a_title'][:40]} × {m['b_title'][:40]}")
    return votes, titles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", default="48", help="which chunk x chunk battery: 48 or 64")
    args = ap.parse_args()
    glob = "default_agg-gpt-5.6-luna_N*_cc*_gpt-5.6-luna_run1"
    dv, dt = people_pairs(glob, "match")
    pv, pt = people_pairs(glob.replace("default", "prosaic"), "match_prosaic")
    both = set(dv) & set(pv)
    print(f"default: {len(dv)} people pairs; prosaic: {len(pv)}; in BOTH frames: {len(both)}")
    for k in sorted(both, key=lambda k: -(dv[k] + pv[k])):
        print(f"  {k[0]:<16} × {k[1]:<16}  default x{dv[k]}: {dt[k][0]}")
        print(f"  {'':<16}   {'':<16}  prosaic x{pv[k]}: {pt[k][0]}")


if __name__ == "__main__":
    main()
