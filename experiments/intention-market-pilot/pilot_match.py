"""Single-window complementarity matching over per-account intention lists.

People are anonymised as P01, P02, … in the prompt (ids P03-2), so the model
cannot lean on background knowledge about a handle. Every returned pair is
checked mechanically: both ids exist and belong to different people. Degree
counts per intention and per person are the generic-intention detector."""
import argparse
import collections
import json

from pilot_common import (AGG_DIR, FRAME, HERE, LUNA, MATCH_DIR, RAW_DIR, accounts, api_keys, app, complete,
                          jdump, jload, o200k_len, parse_blocks)

MAX_OUT = 24000


def lists_text(people):
    parts = []
    for p in people:
        lines = [f"### {p['label']}"]
        for b in p["blocks"]:
            status = f", {b['status']}" if b.get("status") else ""
            lines.append(f"[{b['id']}] ({b['group']}{status}) {b['title']} — {b['body']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=16, help="first N accounts")
    ap.add_argument("--model", default=LUNA)
    ap.add_argument("--agg-model", default=LUNA, help="which aggregation to use as input")
    ap.add_argument("--raw", action="store_true",
                    help="use raw chunk-level lists instead of aggregated ones")
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--people", default=None,
                    help="explicit 0-based index ranges into accounts.json, e.g. '0-15,32-47' "
                         "(chunk x chunk windows); overrides --count")
    ap.add_argument("--exhaustive", action="store_true",
                    help="append an instruction to list every qualifying pair, not a dozen")
    args = ap.parse_args()

    template = (HERE / "prompts/match.txt").read_text(encoding="utf-8")
    people, idmap = [], {}
    acc = accounts()
    if args.people:
        sel = []
        for part in args.people.split(","):
            lo, hi = (part.split("-") + [part])[:2]
            sel += list(range(int(lo), int(hi) + 1))
        chosen = [(i, acc[i]) for i in sel]
    else:
        chosen = list(enumerate(acc[:args.count]))
    for k, (idx, r) in enumerate(chosen, 1):
        h, label = r["username"], f"P{k:02d}"
        if args.raw:
            chunks = sorted([jload(p) for p in RAW_DIR.glob(f"{h}_c*.json")],
                            key=lambda c: c["chunk"])
            blocks = [b for c in chunks for b in parse_blocks(c["content"])]
        else:
            a = jload(AGG_DIR / f"{h}.{args.agg_model}.json")
            blocks = a["blocks"] if a else []
        if not blocks:
            print(f"  {h}: no lists, skipped")
            continue
        pb = []
        for i, b in enumerate(blocks, 1):
            pid = f"{label}-{i}"
            idmap[pid] = {"handle": h, "title": b["title"], "group": b["group"], "body": b["body"]}
            pb.append({**b, "id": pid})
        people.append({"label": label, "handle": h, "blocks": pb})

    prompt = template.replace("{lists}", lists_text(people))
    if args.exhaustive:
        prompt = prompt.replace(
            "There is no target number. If nothing qualifies, output nothing. Quality over quantity: "
            "every match you output should be one you would be willing to explain to both people.",
            "Be exhaustive: there may be dozens of qualifying pairs across this many people, and a "
            "pair you leave out is lost. List every pair that meets the bar above, ordered by "
            "confidence; do not stop at a round number. If nothing qualifies, output nothing.")
    ntok = o200k_len(prompt)
    src = "raw" if args.raw else f"agg-{args.agg_model}"
    extra = (f"_cc{args.people.replace(',', '+')}" if args.people else "") + ("_ex" if args.exhaustive else "")
    tag = f"{FRAME}_{src}_N{len(people)}{extra}_{args.model}_run{args.run}"
    print(f"{len(people)} people, {len(idmap)} intentions, prompt {ntok:,} tokens → {tag}",
          flush=True)

    flask_app = app()
    keys = api_keys(flask_app)
    with flask_app.app_context():
        r = complete(flask_app, keys, args.model, prompt, MAX_OUT, note=f"match {tag}")

    matches, bad = [], []
    for line in r["content"].splitlines():
        line = line.strip().strip("`").strip()
        if not line.startswith("{"):
            continue
        try:
            m = json.loads(line)
        except Exception:  # noqa: BLE001
            bad.append(line[:160])
            continue
        a, b = idmap.get(str(m.get("a"))), idmap.get(str(m.get("b")))
        m["a_ok"], m["b_ok"] = a is not None, b is not None
        m["cross_person"] = bool(a and b and a["handle"] != b["handle"])
        m["grounded_ids"] = bool(a and b) and m["cross_person"]
        if a:
            m["a_handle"], m["a_title"] = a["handle"], a["title"]
        if b:
            m["b_handle"], m["b_title"] = b["handle"], b["title"]
        matches.append(m)
    ok = [m for m in matches if m["grounded_ids"]]
    deg_i = collections.Counter([m["a"] for m in ok] + [m["b"] for m in ok])
    deg_p = collections.Counter([m["a_handle"] for m in ok] + [m["b_handle"] for m in ok])
    summary = {
        "tag": tag, "people": len(people), "intentions": len(idmap), "prompt_tokens": ntok,
        "matches_total": len(matches), "matches_grounded": len(ok),
        "bad_id_matches": len(matches) - len(ok), "unparseable_lines": len(bad),
        "input_tokens": r.get("input_tokens"), "output_tokens": r.get("output_tokens"),
        "truncated": r.get("truncated"), "elapsed": r.get("elapsed"),
        "mean_confidence": round(sum(float(m.get("confidence") or 0) for m in ok)
                                 / max(1, len(ok)), 2),
        "kinds": collections.Counter(m.get("kind", "?") for m in ok).most_common(),
        "top_intentions_by_degree": deg_i.most_common(10),
        "people_by_degree": deg_p.most_common(),
        "people_with_zero": [p["handle"] for p in people if deg_p[p["handle"]] == 0],
    }
    jdump(MATCH_DIR / f"{tag}.json", {"summary": summary, "matches": matches,
                                          "idmap": idmap, "bad_lines": bad,
                                          "content": r["content"]})
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("top_intentions_by_degree", "people_by_degree")}, indent=1))
    print("top intentions by degree:", deg_i.most_common(5))
    print("people by degree:", deg_p.most_common())


if __name__ == "__main__":
    main()
