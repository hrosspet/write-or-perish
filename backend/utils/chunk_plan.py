"""Equal-chunk planning for profile generation (design note 2026-09-03).

Pure arithmetic, no imports: the sync loop, the batch request builder and
the dry-run script all size chunks through ``plan_chunks``.
"""

# Target size of one profile chunk in STORED content units (Node.token_count,
# chars/4 of the node's own text). The planner sizes every chunk of a
# remainder to this target; the real-token cap only ever raises the chunk
# count.
CHUNK_TARGET_UNITS = 90_000


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
