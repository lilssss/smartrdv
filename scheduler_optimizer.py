"""
Scheduling Optimizer — Score-based slot selection
==================================================
Hackathon project · Optimisation sous contraintes pondérées

Architecture
------------
  Slot             → data class representing a candidate time slot
  ScoringEngine    → computes the weighted cost for a single slot
  Optimizer        → ranks all slots and returns the best one
  SlotLoader       → loads slots from dict / JSON / CSV

Usage
-----
  python scheduler_optimizer.py              # runs built-in demo
  python scheduler_optimizer.py slots.json  # loads from JSON file

Score formula
-------------
  Score(slot) = w_conflict    * conflict(slot)
              + w_interruption * interruption(slot)
              + w_preference   * (1 - preference(slot))   ← inverted
              + w_travel       * travel(slot)
              + w_fatigue      * fatigue(slot)

  All raw values are in [0, 1].  Lower score = better slot.
  Weights are automatically normalized so they sum to 1.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    """
    A candidate time slot with pre-computed feature values.

    All feature values must be in [0.0, 1.0]:
      - conflict      : 0 = no overlap with existing events, 1 = full conflict
      - interruption  : 0 = no deep-work block disrupted, 1 = heavy disruption
      - preference    : 0 = disliked time, 1 = preferred time  (NOTE: inverted in score)
      - travel        : 0 = no travel needed, 1 = maximum travel cost
      - fatigue       : 0 = light day, 1 = very dense / exhausting day
    """
    time: str
    conflict: float = 0.0
    interruption: float = 0.0
    preference: float = 0.5
    travel: float = 0.0
    fatigue: float = 0.0

    def __post_init__(self):
        for attr in ("conflict", "interruption", "preference", "travel", "fatigue"):
            v = getattr(self, attr)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"Slot '{self.time}': {attr}={v} must be in [0, 1]")


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

@dataclass
class Weights:
    """
    Importance coefficients for each scoring criterion.
    Automatically normalized so they sum to 1.
    All values must be >= 0.
    """
    conflict: float = 0.40
    interruption: float = 0.20
    preference: float = 0.20
    travel: float = 0.10
    fatigue: float = 0.10

    def __post_init__(self):
        self._normalize()

    def _normalize(self):
        total = self.conflict + self.interruption + self.preference + self.travel + self.fatigue
        if total == 0:
            raise ValueError("All weights are zero — cannot normalize.")
        self.conflict     /= total
        self.interruption /= total
        self.preference   /= total
        self.travel       /= total
        self.fatigue      /= total

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

@dataclass
class ScoreDetail:
    """Holds the full breakdown of a slot's score."""
    slot: Slot
    total: float
    contributions: dict = field(default_factory=dict)

    def __repr__(self):
        lines = [f"  {k:<15} {v:.4f}" for k, v in self.contributions.items()]
        return (
            f"Slot: {self.slot.time}\n"
            f"  Total score  : {self.total:.4f}  (lower = better)\n"
            + "\n".join(lines)
        )


class ScoringEngine:
    """
    Computes the weighted cost score for a given slot.

    Score formula:
        S(slot) = w_conflict    * conflict
                + w_interruption * interruption
                + w_preference   * (1 - preference)
                + w_travel       * travel
                + w_fatigue      * fatigue
    """

    def __init__(self, weights: Optional[Weights] = None):
        self.weights = weights or Weights()

    def score(self, slot: Slot) -> ScoreDetail:
        w = self.weights
        contributions = {
            "conflict":     w.conflict     * slot.conflict,
            "interruption": w.interruption * slot.interruption,
            "preference":   w.preference   * (1.0 - slot.preference),   # inverted
            "travel":       w.travel       * slot.travel,
            "fatigue":      w.fatigue      * slot.fatigue,
        }
        total = sum(contributions.values())
        return ScoreDetail(slot=slot, total=total, contributions=contributions)


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------

class Optimizer:
    """
    Ranks all candidate slots and returns the best one (lowest score).

    Algorithm: greedy selection — O(n) in the number of slots.
    For a single-slot placement problem this is optimal.
    """

    def __init__(self, engine: Optional[ScoringEngine] = None):
        self.engine = engine or ScoringEngine()

    def rank(self, slots: list[Slot]) -> list[ScoreDetail]:
        """Returns all slots sorted from best (lowest cost) to worst."""
        if not slots:
            raise ValueError("No slots provided.")
        scored = [self.engine.score(s) for s in slots]
        scored.sort(key=lambda d: d.total)
        return scored

    def best(self, slots: list[Slot]) -> ScoreDetail:
        """Returns the single best slot."""
        return self.rank(slots)[0]


# ---------------------------------------------------------------------------
# Slot loader (JSON / CSV / dict)
# ---------------------------------------------------------------------------

class SlotLoader:
    """Utility to load slots from various sources."""

    @staticmethod
    def from_dicts(data: list[dict]) -> list[Slot]:
        return [Slot(**d) for d in data]

    @staticmethod
    def from_json(path: str) -> list[Slot]:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return SlotLoader.from_dicts(data)

    @staticmethod
    def from_csv(path: str) -> list[Slot]:
        slots = []
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                slots.append(Slot(
                    time=row["time"],
                    conflict=float(row.get("conflict", 0)),
                    interruption=float(row.get("interruption", 0)),
                    preference=float(row.get("preference", 0.5)),
                    travel=float(row.get("travel", 0)),
                    fatigue=float(row.get("fatigue", 0)),
                ))
        return slots


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(ranked: list[ScoreDetail]) -> None:
    print("\n" + "=" * 55)
    print("  SCHEDULING OPTIMIZER — RANKED RESULTS")
    print("=" * 55)
    for i, detail in enumerate(ranked):
        tag = "  ★ RECOMMENDED" if i == 0 else ""
        print(f"\n#{i+1} — {detail.slot.time}{tag}")
        print(f"  Total cost score : {detail.total:.4f}  (lower = better)")
        print("  Breakdown:")
        for criterion, value in detail.contributions.items():
            bar = "█" * int(value * 40)
            print(f"    {criterion:<15} {value:.4f}  {bar}")
    print("\n" + "=" * 55)


# ---------------------------------------------------------------------------
# Built-in demo data
# ---------------------------------------------------------------------------

DEMO_SLOTS = [
    {"time": "Mon 09:00", "conflict": 0.0, "interruption": 0.3, "preference": 0.9, "travel": 0.2, "fatigue": 0.2},
    {"time": "Mon 11:30", "conflict": 0.0, "interruption": 0.7, "preference": 0.7, "travel": 0.2, "fatigue": 0.4},
    {"time": "Mon 14:00", "conflict": 0.3, "interruption": 0.5, "preference": 0.6, "travel": 0.4, "fatigue": 0.6},
    {"time": "Tue 10:00", "conflict": 0.0, "interruption": 0.2, "preference": 0.8, "travel": 0.1, "fatigue": 0.1},
    {"time": "Tue 16:00", "conflict": 0.1, "interruption": 0.4, "preference": 0.5, "travel": 0.6, "fatigue": 0.7},
    {"time": "Wed 09:30", "conflict": 0.0, "interruption": 0.3, "preference": 0.8, "travel": 0.3, "fatigue": 0.3},
]

DEMO_WEIGHTS = Weights(
    conflict=0.40,
    interruption=0.20,
    preference=0.20,
    travel=0.10,
    fatigue=0.10,
)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def run_tests() -> None:
    """Minimal self-tests — run with: python scheduler_optimizer.py --test"""
    print("Running tests...")

    # Test 1: perfect slot (all zeros, max preference)
    s = Slot(time="test", conflict=0, interruption=0, preference=1.0, travel=0, fatigue=0)
    engine = ScoringEngine(Weights(conflict=1, interruption=0, preference=0, travel=0, fatigue=0))
    detail = engine.score(s)
    assert detail.total == 0.0, f"Expected 0.0, got {detail.total}"

    # Test 2: worst slot
    s2 = Slot(time="bad", conflict=1, interruption=1, preference=0, travel=1, fatigue=1)
    engine2 = ScoringEngine(Weights(conflict=0.25, interruption=0.25, preference=0.25, travel=0.125, fatigue=0.125))
    detail2 = engine2.score(s2)
    assert abs(detail2.total - 1.0) < 1e-6, f"Expected 1.0, got {detail2.total}"

    # Test 3: optimizer picks minimum
    slots = SlotLoader.from_dicts(DEMO_SLOTS)
    opt = Optimizer(ScoringEngine(DEMO_WEIGHTS))
    best = opt.best(slots)
    ranked = opt.rank(slots)
    assert best.slot.time == ranked[0].slot.time

    # Test 4: weight normalization
    w = Weights(conflict=2, interruption=2, preference=2, travel=2, fatigue=2)
    total = w.conflict + w.interruption + w.preference + w.travel + w.fatigue
    assert abs(total - 1.0) < 1e-6, f"Weights should sum to 1, got {total}"

    print("All tests passed.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--test" in args:
        run_tests()
        sys.exit(0)

    if args and not args[0].startswith("--"):
        path = args[0]
        if path.endswith(".json"):
            slots = SlotLoader.from_json(path)
        elif path.endswith(".csv"):
            slots = SlotLoader.from_csv(path)
        else:
            print(f"Unsupported file format: {path}")
            sys.exit(1)
        weights = DEMO_WEIGHTS
    else:
        slots = SlotLoader.from_dicts(DEMO_SLOTS)
        weights = DEMO_WEIGHTS

    engine = ScoringEngine(weights)
    optimizer = Optimizer(engine)
    ranked = optimizer.rank(slots)

    print_report(ranked)

    print("\nWeights used (normalized):")
    for k, v in weights.as_dict().items():
        print(f"  {k:<15} {v:.3f}")
