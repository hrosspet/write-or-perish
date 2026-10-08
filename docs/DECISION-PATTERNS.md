# Patterns in the builder's misses

Analysis of the misses in `DECISION-LOG.md`, which holds only the raw entries. The builder reads this file before predicting Peter's answer or deciding whether to ask, and adds a pattern when one shows in the log. The rates themselves come from `python3 ~/.claude/skills/decision-log/stats.py docs/DECISION-LOG.md`.

- **Overconfidence.** In the 2026-10-02 predictions, at 75–80 % confidence the builder scored 2.5 of 4; at 55–65 %, 2.5 of 5; at 40 %, 0 of 1: about 15 points overconfident.
- **Hard lines read as conditions.** Where Peter holds a rule without exceptions ("never change providers", "never decrypt users' content"), the prediction was a yes with conditions.
- **A cost emergency generalised into a cost mandate.** The 2026-09-12 digest incident was a fix for runaway spend, not a standing wish to cut cost. In this phase he puts product quality first.
- **The builder's own proposals used as evidence of his view.** "Consenting alpha users" came from the builder's triage note, not from him.
- **Assumed constraints instead of checked ones.** A social reason was guessed for not contacting reporters; the real limit was that no channel exists.
