# Intention-market pilot (lab.loore.org, 2026-09-02)

Script-driven pipeline that ran the first end-to-end intention-matching pilot on the
lab VM: import Community Archive accounts through the app's prefill path → chunked
intentions extraction → per-account aggregation → single-window / chunk × chunk
complementarity matching → unions, consensus and degree analysis.

Results (numbers only) are in `../cheap-model-intentions/RESEARCH_SUMMARY.md` §13.
**All content outputs are private and live outside git** (`~/lab-out` on the lab,
`~/data/loore/lab-out-<date>/` locally). Nothing here contains anyone's data.

## Running

On the lab (checkout on the experiment branch, `.env.production` present):

```bash
cp pilot_*.py ~/lab-scripts/ && cp prompts/*.txt ~/lab-scripts/prompts/
~/lab-scripts/run.sh pilot_select.py                       # census → accounts.json (middle tier, seeded shuffle)
~/lab-scripts/run.sh pilot_import.py --start 0 --count 16   # prefill through the app, no profile seeding
~/lab-scripts/run.sh pilot_extract.py --count 16 --mode sync # chunked extraction (public prompt)
~/lab-scripts/run.sh pilot_aggregate.py --count 16          # per-account dedupe with slice metadata
~/lab-scripts/run.sh pilot_match.py --count 16              # single window; --people 0-15,32-47 for chunk × chunk
~/lab-scripts/run.sh pilot_union.py --glob "default_agg-gpt-5.6-luna_N*_cc*_run1"
~/lab-scripts/run.sh pilot_report.py                        # summary.md (numbers only)
```

`run.sh` is a 6-line wrapper that activates the conda env, loads `.env.production`
and sets `PYTHONPATH` (see the report for its text). Environment knobs:
`PILOT_OUT` (output root), `PILOT_FRAME=prosaic` (second extraction frame, separate
raw/agg/match dirs), `PILOT_ROOT` (repo checkout).

Viewers for the qualitative read: `pilot_show.py` (lists), `pilot_show_matches.py <tag>`
(pairs with rationales), `pilot_compare.py <tag> <tag>`, `pilot_crossframe.py`.

## Guardrails built in

- Spend ledger per provider (`spend.json`), hard stop below the agreed caps.
- Every call records `truncated`; the app's 10K output clamp is lifted per process.
- People are anonymised as P01… in matching prompts; every returned id is checked.
- Idempotent: rerunning any stage skips outputs that already exist.
