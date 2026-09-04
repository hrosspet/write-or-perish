"""plan_chunks: equal chunks over a remainder, no leftover tail, cap only
raises the count (design note 2026-09-03)."""
import pytest

from backend.utils.chunk_plan import plan_chunks


T = 90_000


def test_example_from_the_design_note():
    # 350k of pre-fill data: not 3 x 100k + 50k left over, but 4 x 87.5k.
    k, size = plan_chunks(350_000, target=100_000)
    assert k == 4
    assert size == 87_500


def test_round_half_up_not_bankers():
    assert plan_chunks(350_000, target=100_000)[0] == 4  # 3.5 -> 4
    assert plan_chunks(250_000, target=100_000)[0] == 3  # 2.5 -> 3


def test_rounds_down_below_half():
    k, size = plan_chunks(340_000, target=100_000)
    assert k == 3
    assert size == pytest.approx(113_333.3)


def test_single_chunk_takes_the_whole_remainder():
    assert plan_chunks(95_000, target=T) == (1, 95_000)
    assert plan_chunks(130_000, target=T) == (1, 130_000)   # 1.44 -> 1
    assert plan_chunks(40_000, target=T) == (1, 40_000)     # tiny corpus


def test_zero_remainder():
    assert plan_chunks(0) == (0, 0)
    assert plan_chunks(None) == (0, 0)


def test_every_chunk_inside_the_guaranteed_band():
    for remaining in range(1, 2_000_000, 7_919):
        k, size = plan_chunks(remaining, target=T)
        if k >= 2:
            assert T * (1 - 1 / (2 * k)) <= size < T * (1 + 1 / (2 * k))
        assert size * k == pytest.approx(remaining)  # full coverage


def test_cap_only_raises_the_count():
    # A single 130k chunk would render past the cap that allows 100k units.
    k, size = plan_chunks(130_000, target=T, max_units=100_000)
    assert k == 2
    assert size == 65_000
    # Cap looser than the plan: unchanged.
    assert plan_chunks(350_000, target=100_000, max_units=200_000) == (4, 87_500)


def test_cap_forced_split_applies_to_the_whole_remainder():
    # 5 chunks would be 100k each; the cap allows 80k -> 7 equal chunks.
    k, size = plan_chunks(500_000, target=100_000, max_units=80_000)
    assert k == 7
    assert size == pytest.approx(500_000 / 7)
    assert size <= 80_000


def test_next_window_budget_over_asks_only_the_final_chunk():
    from backend.utils.chunk_plan import (
        next_window_budget, FINAL_CHUNK_OVERASK_UNITS)
    # Mid-remainder windows ask for the planned size.
    assert next_window_budget(350_000, target=100_000) == (4, 87_500, 87_500)
    # The final window over-asks so the builder's header allowance and
    # strict fit cannot leave a sliver behind.
    assert next_window_budget(95_000, target=T) == (
        1, 95_000, 95_000 + FINAL_CHUNK_OVERASK_UNITS)
    assert next_window_budget(0) == (0, 0, 0)
    # A cap-forced split changes the budget the way it changes the plan.
    assert next_window_budget(130_000, target=T, max_units=100_000) == (
        2, 65_000, 65_000)


def test_max_units_for_cap():
    from backend.utils.chunk_plan import max_units_for_cap, CAP_MARGIN
    assert max_units_for_cap(272_000, 2.0) == pytest.approx(
        (1 - CAP_MARGIN) * 272_000 / 2)
    assert max_units_for_cap(272_000, 2.0, margin=0) == 136_000
    # Unknown cap or ratio: no cap on the plan.
    assert max_units_for_cap(None, 2.0) is None
    assert max_units_for_cap(272_000, 0) is None


def test_next_build_threshold_walks_the_provisional_ladder():
    from backend.utils.chunk_plan import next_build_threshold, PROVISIONAL_THRESHOLDS
    assert PROVISIONAL_THRESHOLDS == (5_000, 10_000, 15_000, 25_000, 50_000)
    assert next_build_threshold(0) == 5_000        # the minimum for any profile
    assert next_build_threshold(None) == 5_000
    assert next_build_threshold(7_000) == 10_000
    assert next_build_threshold(10_000) == 15_000  # a step is crossed strictly
    assert next_build_threshold(26_000) == 50_000
    assert next_build_threshold(60_000) == T       # the first non-provisional build
    assert next_build_threshold(T) is None         # from here the 80k gate applies
