"""Scoring system for Survivor rankings.

Scoring rules:
- Each contestant is ranked 1-24 by the user (1 = predicted winner, 24 = predicted first out)
- When a contestant leaves the game (by elimination or removal), they receive an
  elimination_order representing their departure position in a unified sequence:
  1 = first to leave, 24 = last (winner). Removals occupy slots in the same sequence
  as eliminations, so the admin enters departure numbers counting all departures.
- finish_position = total_contestants + 1 - elimination_order
  e.g. departure=1 → finish_position=24, departure=24 (winner) → finish_position=1

Removed contestants (taken out by means other than a vote) receive no points.
Points are only awarded for correctly predicting the order in which contestants
are eliminated by vote.

Final 3 scoring (finish positions 1, 2, 3):
- Winner (finish position 1):
    - Ranked #1 by user → 20 pts
    - Ranked in top 3 (but not #1) → 10 pts
    - Ranked outside top 3 → sliding scale
- Runner-up / 3rd place:
    - Ranked in top 3 by user → 10 pts
    - Ranked outside top 3 → sliding scale

Standard scoring (all other eliminated contestants, and Final 3 contestants
ranked outside the user's top 3):
- Exact match (0 off): 10 pts
- 1 off: 9 pts
- 2 off: 8 pts
- ...
- 9 off: 1 pt
- 10+ off: 0 pts
"""

from typing import Optional, TypedDict


class _RankingInputRequired(TypedDict):
    rank: int
    contestant_name: str
    elimination_order: Optional[int]  # None = still in game


class RankingInput(_RankingInputRequired, total=False):
    """One ranking row passed into the scoring engine.

    Required keys: rank, contestant_name, elimination_order.
    Optional keys default to: is_removed=False, scoring_eligible=True.
    """
    is_removed: bool
    scoring_eligible: bool


class BreakdownEntry(TypedDict):
    """Per-contestant result within a ScoreResult."""
    contestant_name: str
    user_rank: int
    finish_position: Optional[int]   # None if still in game or removed
    elimination_order: Optional[int]
    points: Optional[int]            # None if removed or scoring-ineligible
    max_points: Optional[int]
    is_removed: bool
    is_finalist: bool
    scoring_ineligible: bool         # True for late submissions


class ScoreResult(TypedDict):
    """Return value of calculate_total_score."""
    total_score: int
    max_possible: int
    contestants_scored: int
    total_contestants: int
    breakdown: list[BreakdownEntry]


POINTS_PERFECT = 10
POINTS_PER_OFF = 1
MAX_OFF = 10

POINTS_WINNER = 20
POINTS_FINALIST = 10
FINALIST_THRESHOLD = 3  # finish positions 1-3 are the Final 3


def max_points_for(finish_position: int) -> int:
    """Maximum possible points for a contestant at this finish position."""
    if finish_position == 1:
        return POINTS_WINNER
    elif finish_position <= FINALIST_THRESHOLD:
        return POINTS_FINALIST
    else:
        return POINTS_PERFECT


def calculate_points(user_rank: int, elimination_order: int, total_contestants: int = 24) -> int:
    """Calculate points for a single contestant prediction.

    Args:
        user_rank: The user's predicted rank (1 = winner, 24 = first out)
        elimination_order: Departure position in unified sequence (1 = first out, 24 = winner)
        total_contestants: Total number of contestants (fixed at 24)

    Returns:
        Points earned
    """
    finish_position = total_contestants + 1 - elimination_order

    if finish_position == 1:
        if user_rank == 1:
            return POINTS_WINNER
        elif user_rank <= FINALIST_THRESHOLD:
            return POINTS_FINALIST

    elif finish_position <= FINALIST_THRESHOLD:
        if user_rank <= FINALIST_THRESHOLD:
            return POINTS_FINALIST

    # Standard sliding-scale scoring.
    difference = abs(user_rank - finish_position)
    return max(0, POINTS_PERFECT - difference * POINTS_PER_OFF)


def calculate_total_score(
    rankings: list[RankingInput], total_contestants: int = 24
) -> ScoreResult:
    """Calculate total score for all rankings.

    Args:
        rankings: Sequence of RankingInput entries (see type definition above).
                  Rankings with scoring_eligible=False receive no points (late submission).
        total_contestants: Total number of contestants (fixed at 24)

    Returns:
        ScoreResult with total_score, max_possible, and per-contestant breakdown
    """
    breakdown = []
    total_score = 0
    scored_count = 0
    max_possible = 0

    for r in rankings:
        if r.get("is_removed"):
            breakdown.append({
                "contestant_name": r["contestant_name"],
                "user_rank": r["rank"],
                "finish_position": None,
                "elimination_order": None,
                "points": None,
                "max_points": None,
                "is_removed": True,
                "is_finalist": False,
                "scoring_ineligible": False,
            })
        elif not r.get("scoring_eligible", True):
            # Late submission: contestant had already departed when the user submitted.
            # Show actual game data but award no points.
            eo = r.get("elimination_order")
            finish_position = (total_contestants + 1 - eo) if eo is not None else None
            breakdown.append({
                "contestant_name": r["contestant_name"],
                "user_rank": r["rank"],
                "finish_position": finish_position,
                "elimination_order": eo,
                "points": None,
                "max_points": None,
                "is_removed": False,
                "is_finalist": False,
                "scoring_ineligible": True,
            })
        elif r["elimination_order"] is not None:
            finish_position = total_contestants + 1 - r["elimination_order"]
            is_finalist = finish_position <= FINALIST_THRESHOLD
            max_pts = max_points_for(finish_position)
            points = calculate_points(r["rank"], r["elimination_order"], total_contestants)
            total_score += points
            max_possible += max_pts
            scored_count += 1
            breakdown.append({
                "contestant_name": r["contestant_name"],
                "user_rank": r["rank"],
                "finish_position": finish_position,
                "elimination_order": r["elimination_order"],
                "points": points,
                "max_points": max_pts,
                "is_removed": False,
                "is_finalist": is_finalist,
                "scoring_ineligible": False,
            })
        else:
            breakdown.append({
                "contestant_name": r["contestant_name"],
                "user_rank": r["rank"],
                "finish_position": None,
                "elimination_order": None,
                "points": None,
                "max_points": None,
                "is_removed": r.get("is_removed", False),
                "is_finalist": False,
                "scoring_ineligible": False,
            })

    return {
        "total_score": total_score,
        "max_possible": max_possible,
        "contestants_scored": scored_count,
        "total_contestants": total_contestants,
        "breakdown": breakdown,
    }
