"""Unit tests for the scoring engine (app/scoring.py)."""

import random
import pytest
from app.scoring import calculate_points, calculate_total_score, max_points_for

TOTAL = 24  # Season 50 contestant count


# ---------------------------------------------------------------------------
# max_points_for
# ---------------------------------------------------------------------------

def test_max_points_winner():
    assert max_points_for(1) == 20

def test_max_points_finalist():
    assert max_points_for(2) == 10
    assert max_points_for(3) == 10

def test_max_points_standard():
    assert max_points_for(4) == 10
    assert max_points_for(24) == 10


# ---------------------------------------------------------------------------
# calculate_points — standard sliding scale
# ---------------------------------------------------------------------------

def test_perfect_standard():
    # departure=1 (first out) → finish_position=24
    # user ranked them #24 → exactly right
    assert calculate_points(24, 1, TOTAL) == 10

def test_one_off():
    assert calculate_points(23, 1, TOTAL) == 9
    assert calculate_points(24, 2, TOTAL) == 9  # finish_pos=23, user said 24

def test_nine_off():
    assert calculate_points(15, 1, TOTAL) == 1  # 9 off from finish_pos 24

def test_ten_off_and_beyond():
    assert calculate_points(14, 1, TOTAL) == 0
    assert calculate_points(1, 1, TOTAL) == 0   # 23 off


# ---------------------------------------------------------------------------
# calculate_points — Final 3 scoring
# ---------------------------------------------------------------------------

def test_winner_correct_pick():
    # departure=24 → finish_position=1 (winner)
    assert calculate_points(1, 24, TOTAL) == 20

def test_winner_top3_pick():
    assert calculate_points(2, 24, TOTAL) == 10
    assert calculate_points(3, 24, TOTAL) == 10

def test_winner_outside_top3():
    # rank=4, finish_position=1 — falls to sliding scale: |4-1|=3 off → 7 pts
    assert calculate_points(4, 24, TOTAL) == 7

def test_finalist_top3_pick():
    # departure=23 → finish_position=2 (runner-up)
    assert calculate_points(1, 23, TOTAL) == 10
    assert calculate_points(3, 23, TOTAL) == 10

def test_finalist_outside_top3():
    # rank=4, finish_position=2 — sliding scale: |4-2|=2 off → 8 pts
    assert calculate_points(4, 23, TOTAL) == 8


# ---------------------------------------------------------------------------
# calculate_points — removals do not affect scoring of other contestants
# ---------------------------------------------------------------------------

def test_removal_does_not_widen_window():
    # departure=3 → finish_pos=22; rank 23 is 1 off → 9 pts (no window bonus)
    assert calculate_points(22, 3, TOTAL) == 10  # exact match
    assert calculate_points(23, 3, TOTAL) == 9    # 1 off
    assert calculate_points(21, 3, TOTAL) == 9    # 1 off


# ---------------------------------------------------------------------------
# calculate_total_score
# ---------------------------------------------------------------------------

def _make_rankings(specs):
    """Build ranking dicts from (rank, elimination_order, is_removed, scoring_eligible) tuples."""
    return [
        {
            "rank": rank,
            "elimination_order": eo,
            "contestant_name": f"Contestant {i}",
            "is_removed": is_removed,
            "scoring_eligible": eligible,
        }
        for i, (rank, eo, is_removed, eligible) in enumerate(specs, 1)
    ]


def test_total_score_no_departures():
    # No eliminations yet — no one scores
    rankings = _make_rankings([(i, None, False, True) for i in range(1, 25)])
    result = calculate_total_score(rankings, TOTAL)
    assert result["total_score"] == 0
    assert result["max_possible"] == 0
    assert result["contestants_scored"] == 0


def test_total_score_single_perfect():
    specs = [(24, 1, False, True)] + [(i, None, False, True) for i in range(1, 24)]
    result = calculate_total_score(_make_rankings(specs), TOTAL)
    assert result["total_score"] == 10
    assert result["max_possible"] == 10
    assert result["contestants_scored"] == 1


def test_total_score_removed_contestant_no_points():
    # Removed contestant should contribute no points and no max_possible
    specs = [(5, 1, True, True)] + [(i, None, False, True) for i in range(1, 24)]
    result = calculate_total_score(_make_rankings(specs), TOTAL)
    assert result["total_score"] == 0
    assert result["max_possible"] == 0
    removed_entry = next(b for b in result["breakdown"] if b["is_removed"])
    assert removed_entry["points"] is None


def test_total_score_late_submission_no_points():
    # scoring_eligible=False means the contestant had already left; no points awarded
    specs = [(24, 1, False, False)] + [(i, None, False, True) for i in range(1, 24)]
    result = calculate_total_score(_make_rankings(specs), TOTAL)
    assert result["total_score"] == 0
    ineligible = next(b for b in result["breakdown"] if b.get("scoring_ineligible"))
    assert ineligible["finish_position"] == 24  # game data still shown


def test_total_score_removal_no_window_bonus():
    # 1 removal (departure=1) before elimination (departure=2, finish_pos=23)
    # user rank 24 is 1 off from finish_pos 23 → 9 pts (no window bonus)
    specs = [
        {"rank": 24, "elimination_order": 1, "contestant_name": "Removed", "is_removed": True, "scoring_eligible": True},
        {"rank": 24, "elimination_order": 2, "contestant_name": "Eliminated", "is_removed": False, "scoring_eligible": True},
    ] + [{"rank": i, "elimination_order": None, "contestant_name": f"C{i}", "is_removed": False, "scoring_eligible": True} for i in range(1, 23)]
    result = calculate_total_score(specs, TOTAL)
    elim_entry = next(b for b in result["breakdown"] if b["contestant_name"] == "Eliminated")
    assert elim_entry["points"] == 9


def test_total_score_winner():
    specs = [(1, 24, False, True)] + [(i + 1, None, False, True) for i in range(1, 24)]
    result = calculate_total_score(_make_rankings(specs), TOTAL)
    winner_entry = next(b for b in result["breakdown"] if b["finish_position"] == 1)
    assert winner_entry["points"] == 20
    assert winner_entry["max_points"] == 20


# ---------------------------------------------------------------------------
# Fuzz testing — 100 random scenarios validated against independent reference
# ---------------------------------------------------------------------------

def _reference_score(user_rank, finish_position, is_removed):
    """Independent scoring calculation for fuzz test validation."""
    if is_removed:
        return None
    if finish_position == 1:
        if user_rank == 1:
            return 20
        elif user_rank <= 3:
            return 10
    elif finish_position <= 3:
        if user_rank <= 3:
            return 10
    return max(0, 10 - abs(user_rank - finish_position))


_rng_master = random.Random(42)
_FUZZ_SEEDS = [_rng_master.randint(0, 2**32) for _ in range(1000)]


@pytest.mark.parametrize("seed", _FUZZ_SEEDS)
def test_fuzz_scoring(seed):
    rng = random.Random(seed)
    ranks = list(range(1, TOTAL + 1))
    elim_orders = list(range(1, TOTAL + 1))
    rng.shuffle(ranks)
    rng.shuffle(elim_orders)

    num_removals = rng.randint(0, 3)
    removed_indices = set(rng.sample(range(TOTAL), num_removals))

    rankings = []
    expected_total = 0
    expected_max = 0
    expected_scored = 0

    for i in range(TOTAL):
        is_removed = i in removed_indices
        finish_pos = TOTAL + 1 - elim_orders[i]
        exp_pts = _reference_score(ranks[i], finish_pos, is_removed)

        rankings.append({
            "rank": ranks[i],
            "elimination_order": elim_orders[i],
            "contestant_name": f"C{i}",
            "is_removed": is_removed,
            "scoring_eligible": True,
        })

        if exp_pts is not None:
            expected_total += exp_pts
            expected_scored += 1
            if finish_pos == 1:
                expected_max += 20
            elif finish_pos <= 3:
                expected_max += 10
            else:
                expected_max += 10

    result = calculate_total_score(rankings, TOTAL)

    assert result["total_score"] == expected_total
    assert result["max_possible"] == expected_max
    assert result["contestants_scored"] == expected_scored

    for entry in result["breakdown"]:
        idx = int(entry["contestant_name"][1:])
        is_removed = idx in removed_indices
        finish_pos = TOTAL + 1 - elim_orders[idx]
        exp = _reference_score(ranks[idx], finish_pos, is_removed)
        assert entry["points"] == exp
