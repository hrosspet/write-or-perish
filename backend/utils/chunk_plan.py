"""Equal-chunk planning for profile generation (design note 2026-09-03,
docs/design/chunk-planner.md).

Pure arithmetic, no imports. The synchronous chunk loop, the batch
request builder, the seeding gates and the dry-run script all size
chunks through ``plan_chunks`` / ``next_window_budget`` and share the
constants below, which used to live as four copies across three files.
"""

# T: target size of one profile chunk in STORED content units
# (Node.token_count, chars/4 of the node's own text). The planner sizes
# every chunk of a remainder to this target; the real-token cap only
# ever raises the chunk count. 90k matches the chain's existing cadence
# (hrosspet's 2026 updates carried 59k–106k units each, mean 82k).
CHUNK_TARGET_UNITS = 90_000

# The organic-growth gate: an account whose remaining data is all NEWER
# than its last profile version waits until this many units accumulate
# before the next update. Data OLDER than the last version (a pre-fill,
# an import, a chunk lost to a restart) is an unfinished chain and
# continues regardless — see ``should_continue_chain`` in
# backend/tasks/exports.py.
UPDATE_THRESHOLD_UNITS = 80_000

# μ: fraction of the model's input cap held back when the planner turns
# the cap into a maximum chunk size, covering ratio drift between one
# chunk and the next (tokens per unit moved 2.39 → 1.61 → 1.77 across
# the exgenesis corpus).
CAP_MARGIN = 0.05

# Over-ask on the final chunk of a remainder: the export builder keeps a
# 100-unit header allowance out of its budget and stops BEFORE the row
# that would overshoot, so a window asked at exactly R units lands a few
# nodes short and leaves a sliver for one more (tiny) chunk. A window
# cannot exceed the data, so asking for more costs nothing.
FINAL_CHUNK_OVERASK_UNITS = 10_000


def plan_chunks(remaining_units, target=CHUNK_TARGET_UNITS, max_units=None):
    """Split ``remaining_units`` of content into equal chunks.

    Returns ``(k, size)``: the chunk count and the size of each chunk in
    the same units. ``k`` is the round-half-up of remaining/target, so a
    fixed corpus is always covered with no leftover tail and every chunk
    lands within ``target / (2k)`` of the target. ``max_units`` (the
    largest chunk that fits the model's real-token cap) only ever raises
    ``k``, applied to the whole remainder so cap-forced splits stay equal.

    Pure: call it again after every chunk with the new remainder — the
    result is stable, so no plan needs persisting.
    """
    remaining = max(int(remaining_units or 0), 0)
    if remaining == 0:
        return 0, 0
    k = max(1, int(remaining / target + 0.5))
    if max_units and max_units > 0:
        k = max(k, -(-remaining // int(max_units)))  # ceil
    return k, remaining / k


def max_units_for_cap(input_cap, tokens_per_unit, margin=CAP_MARGIN):
    """Largest chunk, in units, whose prompt stays under ``input_cap`` real
    tokens at ``tokens_per_unit`` (the user's measured or prior ratio,
    which folds in the prompt's fixed parts). None when either figure is
    unknown, which leaves the plan uncapped."""
    if not input_cap or not tokens_per_unit or tokens_per_unit <= 0:
        return None
    return (1 - margin) * input_cap / tokens_per_unit


def next_window_budget(remaining_units, max_units=None,
                       target=CHUNK_TARGET_UNITS):
    """Plan the remainder and return ``(k, size, budget)``: the chunk
    count, the planned chunk size, and the unit budget to hand the export
    builder for the NEXT window. Mid-remainder windows ask for the planned
    size; the final window (k == 1) over-asks so it takes everything."""
    k, size = plan_chunks(remaining_units, target=target, max_units=max_units)
    if k == 0:
        return 0, 0, 0
    if k == 1:
        return 1, size, int(remaining_units) + FINAL_CHUNK_OVERASK_UNITS
    return k, size, int(size)
