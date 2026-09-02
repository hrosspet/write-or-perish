"""Cost breakdown of a pilot run, computed from the output files (each raw chunk,
aggregation and match file records its model and token usage), by
frame × stage × model, plus unit costs for estimating reruns."""
import collections
import statistics

from pilot_common import LUNA, OPUS, OUT, PRICE, jload


def usd(model, i, o, batch=False):
    pi, po = PRICE[model]
    if model == LUNA and (i or 0) > 272_000:
        pi, po = pi * 2, po * 1.5
    return ((i or 0) * pi + (o or 0) * po) / 1e6 * (0.5 if batch else 1)


def main():
    rows = []  # (frame, stage, model, in, out, usd, meta)
    for d in sorted(OUT.glob("raw*")):
        frame = d.name.replace("raw", "").lstrip("_") or "default"
        for p in d.glob("*.json"):
            c = jload(p)
            rows.append((frame, "extract", c["model"], c.get("input_tokens") or 0,
                         c.get("output_tokens") or 0, c.get("batch", False), {"handle": c["handle"]}))
    for d in sorted(OUT.glob("agg*")):
        frame = d.name.replace("agg", "").lstrip("_") or "default"
        for p in d.glob("*.json"):
            a = jload(p)
            rows.append((frame, "aggregate", a["model"], a.get("input_tokens") or 0,
                         a.get("output_tokens") or 0, False, {"handle": a["handle"]}))
    for d in sorted(OUT.glob("match*")):
        frame = d.name.replace("match", "").lstrip("_") or "default"
        for p in d.glob("*.json"):
            s = jload(p)["summary"]
            model = LUNA if LUNA in s["tag"].split("_N")[-1] else OPUS
            rows.append((frame, "match", model, s.get("input_tokens") or 0,
                         s.get("output_tokens") or 0, False,
                         {"people": s["people"], "matches": s["matches_grounded"], "tag": s["tag"]}))

    agg = collections.defaultdict(lambda: [0, 0, 0, 0.0])
    for frame, stage, model, i, o, batch, _ in rows:
        k = (frame, stage, model)
        agg[k][0] += 1
        agg[k][1] += i
        agg[k][2] += o
        agg[k][3] += usd(model, i, o, batch)
    print("| frame | stage | model | calls | input tok | output tok | USD |")
    print("|---|---|---|---|---|---|---|")
    tot = collections.defaultdict(float)
    for (frame, stage, model), (n, i, o, u) in sorted(agg.items()):
        print(f"| {frame} | {stage} | {model} | {n} | {i:,} | {o:,} | {u:.2f} |")
        tot[model] += u
    print(f"\ntotal by provider: {', '.join(f'{m}: ${u:.2f}' for m, u in tot.items())} "
          f"(ledger: {jload(OUT / 'spend.json')})")

    # unit costs
    print("\n## Unit costs (for estimating reruns)")
    for frame in sorted({r[0] for r in rows}):
        for model in (LUNA, OPUS):
            ex = [r for r in rows if r[0] == frame and r[1] == "extract" and r[2] == model]
            if ex:
                per_acc = collections.defaultdict(float)
                for r in ex:
                    per_acc[r[6]["handle"]] += usd(model, r[3], r[4], r[5])
                print(f"- {frame} extraction on {model}: {len(per_acc)} accounts, "
                      f"median ${statistics.median(per_acc.values()):.3f}/account "
                      f"(mean ${statistics.mean(per_acc.values()):.3f}), "
                      f"{len(ex)/len(per_acc):.1f} chunks/account, "
                      f"median {statistics.median(r[3] for r in ex):,.0f} input tok/chunk")
            ag = [r for r in rows if r[0] == frame and r[1] == "aggregate" and r[2] == model]
            if ag:
                print(f"- {frame} aggregation on {model}: {len(ag)} accounts, "
                      f"median ${statistics.median(usd(model, r[3], r[4]) for r in ag):.3f}/account, "
                      f"median {statistics.median(r[3] for r in ag):,.0f} in / "
                      f"{statistics.median(r[4] for r in ag):,.0f} out tok")
        ma = [r for r in rows if r[0] == frame and r[1] == "match"]
        by_n = collections.defaultdict(list)
        for r in ma:
            by_n[(r[2], r[6]["people"])].append(r)
        for (model, n), rs in sorted(by_n.items()):
            c = [usd(model, r[3], r[4]) for r in rs]
            m = [r[6]["matches"] for r in rs]
            print(f"- {frame} match on {model}, N={n}: {len(rs)} calls, "
                  f"${statistics.mean(c):.3f}/call, {statistics.mean(m):.0f} matches/call → "
                  f"${statistics.mean(c) / max(1, statistics.mean(m)):.4f} per match")


if __name__ == "__main__":
    main()
