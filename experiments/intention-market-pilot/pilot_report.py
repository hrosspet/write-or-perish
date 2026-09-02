"""Compile the pilot's numbers into OUT/summary.md (numbers only, safe for the
public research doc) and OUT/report_data.json (everything, private)."""
import collections
import json
import statistics

from pilot_common import OUT, jload
import pathlib


def rows_jsonl(path):
    p = OUT / path
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def main():
    acc = jload(OUT / "accounts.json", [])
    imports = rows_jsonl("imports.jsonl")
    raw = [jload(p) for p in sorted(OUT.glob("raw*/*.json"))]
    for p in sorted(OUT.glob("raw*/*.json")):
        pass
    aggs = [dict(jload(p), frame=p.parent.name.replace("agg", "").lstrip("_") or "default")
            for p in sorted(OUT.glob("agg*/*.json"))]
    matches = [jload(p) for p in sorted(OUT.glob("match*/*.json"))]
    spend = jload(OUT / "spend.json", {})

    L = ["# Intention-market pilot — numbers", ""]
    L += [f"- accounts in tier: {len(acc)}; imported: {sum(1 for i in imports if i.get('ok'))} "
          f"(failed: {sum(1 for i in imports if not i.get('ok'))})"]
    if imports:
        ok = [i for i in imports if i.get("ok")]
        if ok:
            secs = [i["seconds"] for i in ok]
            nodes = [i.get("total") or 0 for i in ok]
            L += [f"- import time per account: median {statistics.median(secs):.0f}s, "
                  f"max {max(secs):.0f}s; nodes per account: median {statistics.median(nodes):,.0f}, "
                  f"total {sum(nodes):,}"]
    if raw:
        by_h = collections.defaultdict(list)
        for c in raw:
            by_h[c["handle"]].append(c)
        blocks = [c.get("n_blocks", 0) for c in raw]
        L += ["", "## Extraction (chunk level)",
              f"- chunks: {len(raw)} over {len(by_h)} accounts; chunks per account: "
              f"median {statistics.median(len(v) for v in by_h.values()):.0f}",
              f"- prompt tokens per chunk: median {statistics.median(c['prompt_tokens'] for c in raw):,.0f}, "
              f"max {max(c['prompt_tokens'] for c in raw):,}",
              f"- intentions per chunk: median {statistics.median(blocks):.0f}, "
              f"min {min(blocks)}, max {max(blocks)}; truncated outputs: "
              f"{sum(1 for c in raw if c.get('truncated'))}",
              f"- output tokens per chunk: median {statistics.median(c.get('output_tokens') or 0 for c in raw):,.0f}"]
    if aggs:
        L += ["", "## Aggregation"]
        for frame, model in sorted({(a["frame"], a["model"]) for a in aggs}):
            am = [a for a in aggs if a["model"] == model and a["frame"] == frame]
            ratios = [a["ratio"] for a in am]
            L += [f"- {frame} / {model}: {len(am)} accounts; raw → aggregated: median "
                  f"{statistics.median(a['raw_count'] for a in am):.0f} → "
                  f"{statistics.median(a['agg_count'] for a in am):.0f} per account "
                  f"(ratio median {statistics.median(ratios):.2f}, min {min(ratios):.2f}, max {max(ratios):.2f}); "
                  f"endorsed share {sum(a['endorsed'] for a in am) / max(1, sum(a['agg_count'] for a in am)):.0%}; "
                  f"truncated: {sum(1 for a in am if a.get('truncated'))}"]
    if matches:
        L += ["", "## Matching (single window)", "",
              "| run | people | intentions | prompt tok | matches | grounded | bad ids | mean conf | people w/ 0 | max degree | truncated |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for m in matches:
            s = m["summary"]
            top = s["top_intentions_by_degree"][0][1] if s["top_intentions_by_degree"] else 0
            L += [f"| {s['tag']} | {s['people']} | {s['intentions']} | {s['prompt_tokens']:,} | "
                  f"{s['matches_total']} | {s['matches_grounded']} | {s['bad_id_matches']} | "
                  f"{s['mean_confidence']} | {len(s['people_with_zero'])} | {top} | {s['truncated']} |"]
    L += ["", "## Spend", f"- {json.dumps(spend)}"]
    (OUT / "summary.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))
    (OUT / "report_data.json").write_text(json.dumps({
        "accounts": acc, "imports": imports, "aggregations": aggs,
        "matches": matches, "spend": spend}, indent=1, default=str))


if __name__ == "__main__":
    main()
