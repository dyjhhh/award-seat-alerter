"""Value policy: which candidates are worth waking the operator for.

The score is a points proxy, not a cents-per-point valuation:

    score = baseline_points * cabin_weight / points

A candidate qualifies only when its exact score is strictly greater than the
threshold. A score exactly at the threshold fails. Rounding is for display and
happens after the comparison, so a score a hair above the threshold is not
rounded down into a tie. Every other gate is plain configuration: a date
window, a fee cap, a minimum seat count and, optionally, a longest connection.

Two rules decide what a gate may do with missing data:

- A known value that fails a gate drops the candidate, with a reason.
- A value the provider did not state is a label on the alert, not a drop.
  The person checks it on the official page. Dropping it silently is how a
  real lead disappears without anyone knowing it existed.

Point balances are not an input. The score uses the award's own price, and
whether to spend points on it is the person's call after the alert.
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
    reasons: tuple[str, ...]           # why it was dropped
    labels: tuple[str, ...] = ()       # what the alert must say is unconfirmed


@dataclass(frozen=True)
class ValuePolicy:
    baseline_points: int
    cabin_weights: dict[str, Decimal]
    threshold: Decimal
    window: tuple[date, date]
    max_fees_cents: int
    currency: str
    min_quantity: int = 1
    max_connection_minutes: int | None = None

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
        # Refused, not ignored: a config that still lists balances expects them to gate.
        legacy = sorted({"funding", "balances"} & set(config))
        if legacy:
            raise PolicyError(f"point balances are not an alert input; remove {legacy}")
        min_quantity = config.get("min_quantity", 1)
        if type(min_quantity) is not int or min_quantity < 1:
            raise PolicyError("min_quantity must be a positive integer")
        max_connection = config.get("max_connection_minutes")
        if max_connection is not None and (type(max_connection) is not int or max_connection < 0):
            raise PolicyError("max_connection_minutes must be a non-negative integer or null")
        return cls(baseline, weights, threshold, window, max_fees,
                   str(config.get("currency", "USD")).upper(), min_quantity, max_connection)

    def exact_score(self, c: Candidate) -> Decimal | None:
        weight = self.cabin_weights.get(c.cabin)
        if weight is None or c.points <= 0:
            return None
        return Decimal(self.baseline_points) * weight / Decimal(c.points)

    def score(self, c: Candidate) -> Decimal | None:
        """Rounded for display only. Comparisons use the exact value."""
        raw = self.exact_score(c)
        return None if raw is None else raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    def evaluate(self, c: Candidate) -> Verdict:
        reasons, labels = [], []
        exact = self.exact_score(c)
        if exact is None:
            reasons.append("cabin_not_scored")
        elif not exact > self.threshold:  # rounding first would let 2.50004 tie 2.50
            reasons.append("score_not_above_threshold")
        if not self.window[0] <= c.start <= self.window[1]:
            reasons.append("outside_window")
        if c.currency is None:
            labels.append("fee_currency_unconfirmed")  # the cap below is applied to the number as shown
        elif c.currency != self.currency:
            reasons.append("fee_currency_mismatch")
        if c.currency in (None, self.currency) and c.fees_cents > self.max_fees_cents:
            reasons.append("fees_over_cap")
        if c.quantity is None:
            labels.append("seats_unconfirmed")
        elif c.quantity < self.min_quantity:
            reasons.append("not_enough_inventory")
        if self.max_connection_minutes is not None and c.kind == "flight":
            if c.connection_minutes is None:
                labels.append("connection_unconfirmed")
            elif c.connection_minutes > self.max_connection_minutes:
                reasons.append("connection_too_long")
        return Verdict(not reasons, self.score(c), tuple(reasons), tuple(labels))
