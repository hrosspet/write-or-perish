"""Compare two match runs over the SAME input lists (same idmap): shared pairs,
pairs unique to each, and agreement on the people involved."""
import argparse

from pilot_common import MATCH_DIR, jload


def key(d, pid):
    it = d["idmap"][str(pid)]
    return f"{it['handle']}:{it['title']}"


def pairs(d):
    """Keyed by (handle:title, handle:title) so runs over the same lists compare
    even if ids were assigned differently."""
    out = {}
    for m in d["matches"]:
        if m.get("grounded_ids"):
            out[tuple(sorted((key(d, m["a"]), key(d, m["b"]))))] = m
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag_a")
    ap.add_argument("tag_b")
    args = ap.parse_args()
    A, B = jload(MATCH_DIR / f"{args.tag_a}.json"), jload(MATCH_DIR / f"{args.tag_b}.json")
    pa, pb = pairs(A), pairs(B)
    shared = set(pa) & set(pb)
    ppl_a = {tuple(sorted((m["a_handle"], m["b_handle"]))) for m in pa.values()}
    ppl_b = {tuple(sorted((m["a_handle"], m["b_handle"]))) for m in pb.values()}
    print(f"A={args.tag_a}: {len(pa)} pairs; B={args.tag_b}: {len(pb)} pairs")
    print(f"shared intention pairs: {len(shared)}  (Jaccard {len(shared) / max(1, len(set(pa) | set(pb))):.2f})")
    print(f"shared PEOPLE pairs: {len(ppl_a & ppl_b)} of A {len(ppl_a)} / B {len(ppl_b)}")
    for name, only in (("only in A", set(pa) - shared), ("only in B", set(pb) - shared), ("shared", shared)):
        print(f"\n{name}:")
        for a, b in sorted(only):
            print(f"  {a[:52]:<52} × {b[:52]}")


if __name__ == "__main__":
    main()
