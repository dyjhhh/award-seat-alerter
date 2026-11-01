"""Value policy: which candidates are worth waking the operator for.

The score is a points proxy, not a cents-per-point valuation:

    score = baseline_points * cabin_weight / points

A candidate qualifies only when its exact score is strictly greater than the
threshold. A score exactly at the threshold fails. Rounding is for display and
happens after the comparison, so a score a hair above the threshold is not
rounded down into a tie. Every other gate is plain
configuration: a date window, a fee cap, and funding from named point pools.
A pool with no known balance is unknown, never assumed to be enough.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, InvalidOperation

from candidates import Candidate


class PolicyError(ValueError):
    """Configuration that cannot be applied safely."""


@dataclass(frozen=True)
class Verdict:
    eligible: bool
    score: Decimal | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ValuePolicy:
    baseline_points: int
    cabin_weights: dict[str, Decimal]
    threshold: Decimal
    window: tuple[date, date]
    max_fees_cents: int
    currency: str
    funding: dict[str, tuple[str, ...]]     # program -> pools that can pay for it
    balances: dict[str, int | None]         # pool -> points, None when unknown
    min_quantity: int = 1

    @classmethod
    def from_config(cls, config: dict) -> "ValuePolicy":
        """Validate a plain dict. Anything malformed raises; nothing is guessed."""
        if not isinstance(config, dict):
            raise PolicyError("config must be an object")
        try:
            weights = {str(k).title(): Decimal(str(v)) for k, v in config["cabin_weights"].items()}
            threshold = Decimal(str(config["threshold"]))
            window = (date.fromisoformat(config["window"][0]), date.fromisoformat(config["window"][1]))
            baseline = config["baseline_points"]
            max_fees = config["max_fees_cents"]
        except (KeyError, TypeError, ValueError, InvalidOperation, IndexError) as exc:
            raise PolicyError(f"config incomplete: {exc}") from None
        if type(baseline) is not int or baseline <= 0:
            raise PolicyError("baseline_points must be a positive integer")
        if type(max_fees) is not int or max_fees < 0:
            raise PolicyError("max_fees_cents must be a non-negative integer")
        if not weights or any(w <= 0 for w in weights.values()) or threshold <= 0:
            raise PolicyError("weights and threshold must be positive")
        if window[1] < window[0]:
            raise PolicyError("window ends before it starts")
        funding = {str(k): tuple(v) for k, v in config.get("funding", {}).items()}
        balances = {}
        for pool, value in config.get("balances", {}).items():
            if value is not None and (type(value) is not int or value < 0):
                raise PolicyError(f"balance for {pool} must be a non-negative integer or null")
            balances[str(pool)] = value
        min_quantity = config.get("min_quantity", 1)
        if type(min_quantity) is not int or min_quantity < 1:
            raise PolicyError("min_quantity must be a positive integer")
        return cls(baseline, weights, threshold, window, max_fees,
                   str(config.get("currency", "USD")).upper(), funding, balances, min_quantity)

    def exact_score(self, c: Candidate) -> Decimal | None:
        weight = self.cabin_weights.get(c.cabin)
        if weight is None or c.points <= 0:
            return None
        return Decimal(self.baseline_points) * weight / Decimal(c.points)

    def score(self, c: Candidate) -> Decimal | None:
        """Rounded for display only. Comparisons use the exact value."""
        raw = self.exact_score(c)
        return None if raw is None else raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    def funding_problem(self, c: Candidate) -> str | None:
        pools = self.funding.get(c.program)
        if not pools:
            return "program_not_fundable"
        need = c.points * self.min_quantity
        if c.kind == "stay" and c.end is not None:
            need *= (c.end - c.start).days
        known = 0
        for pool in pools:
            if pool not in self.balances or self.balances[pool] is None:
                return "balance_unknown"
            known += self.balances[pool]
        return None if known >= need else "insufficient_funding"

    def evaluate(self, c: Candidate) -> Verdict:
        reasons = []
        exact = self.exact_score(c)
        if exact is None:
            reasons.append("cabin_not_scored")
        elif not exact > self.threshold:  # rounding first would let 2.50004 tie 2.50
            reasons.append("score_not_above_threshold")
        if not self.window[0] <= c.start <= self.window[1]:
            reasons.append("outside_window")
        if c.currency != self.currency:
            reasons.append("fee_currency_mismatch")
        elif c.fees_cents > self.max_fees_cents:
            reasons.append("fees_over_cap")
        if c.quantity < self.min_quantity:
            reasons.append("not_enough_inventory")
        problem = self.funding_problem(c)
        if problem:
            reasons.append(problem)
        return Verdict(not reasons, self.score(c), tuple(reasons))
