"""Exact money representation for RECLAIM.

CONTRACT DECISION (see CONTRACTS.md, JC-01)
-------------------------------------------
Money is stored as an **integer number of paise**, never as float and never as a
bare ``Decimal`` on the wire.  HACKATHON_PLAN.md §5 requires "plain code with
decimal types" for all money arithmetic; integer paise satisfies that intent more
strictly than ``Decimal`` because:

1. The hash-chained audit log (§15) needs a *canonical* byte serialisation.
   ``Decimal("1499.0")`` and ``Decimal("1499.00")`` are equal but serialise
   differently, which would silently break the chain.  ``149900`` does not.
2. Invariant #6 ("total recovered per obligation <= amount owed") is an exact
   integer comparison with no epsilon.
3. Net-incremental-recovery sums over thousands of cases stay exact.

``Decimal`` is still the *input* type: ``Money.from_rupees(Decimal("1499.00"))``.
Ratios (recovery rate, cost per rupee) are **not** floats either: every ratio that
is *reported* is a quantised ``Decimal`` from ``units.py``, because ratios travel
beside money in the same audit rows and ``canonical_json`` rejects floats anywhere
in a payload (JC-15, JC-34). ``Money.ratio_to`` returns a full-precision
``Decimal`` on purpose -- it is an intermediate, and quantising per term instead of
once at the reporting boundary is how a stratum-weighted sum drifts.

All rounding in this module is ``ROUND_HALF_UP`` at the paise boundary.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Iterable, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

try:  # pragma: no cover - 3.11+
    from enum import StrEnum
except ImportError:  # pragma: no cover - 3.10 fallback
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Minimal StrEnum shim."""


__all__ = [
    "Currency",
    "Money",
    "PAISE_PER_RUPEE",
    "ROUNDING",
    "money_sum",
]

PAISE_PER_RUPEE = 100
ROUNDING = ROUND_HALF_UP

_RupeeInput = Union[int, str, Decimal]


class Currency(StrEnum):
    """Currencies RECLAIM recognises.

    The hackathon build is INR-only; the field exists so that a second currency
    cannot be introduced by silently reinterpreting an integer.
    """

    INR = "INR"


class Money(BaseModel):
    """An exact monetary amount, stored in the currency's minor unit (paise).

    Frozen and hashable so it can appear inside stratum keys and frozen
    action-parameter models.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    paise: int = Field(description="Amount in the minor unit. May be negative (costs, credits).")
    currency: Currency = Currency.INR

    # ---------------------------------------------------------------- builders

    @classmethod
    def zero(cls, currency: Currency = Currency.INR) -> "Money":
        return cls(paise=0, currency=currency)

    @classmethod
    def from_paise(cls, paise: int, currency: Currency = Currency.INR) -> "Money":
        return cls(paise=int(paise), currency=currency)

    @classmethod
    def from_rupees(cls, rupees: _RupeeInput, currency: Currency = Currency.INR) -> "Money":
        """Build from a rupee amount. Floats are rejected on purpose."""
        if isinstance(rupees, float):  # pragma: no cover - defensive
            raise TypeError(
                "Money.from_rupees() refuses float input; pass Decimal or str "
                "to avoid binary-rounding drift."
            )
        dec = Decimal(rupees) if not isinstance(rupees, Decimal) else rupees
        quantised = (dec * PAISE_PER_RUPEE).quantize(Decimal(1), rounding=ROUNDING)
        return cls(paise=int(quantised), currency=currency)

    # -------------------------------------------------------------- accessors

    @property
    def rupees(self) -> Decimal:
        """Exact rupee value as a Decimal. For display and reporting only."""
        return (Decimal(self.paise) / PAISE_PER_RUPEE).quantize(
            Decimal("0.01"), rounding=ROUNDING
        )

    @property
    def is_zero(self) -> bool:
        return self.paise == 0

    @property
    def is_positive(self) -> bool:
        return self.paise > 0

    # -------------------------------------------------------------- arithmetic

    def _assert_same_currency(self, other: "Money") -> None:
        if self.currency is not other.currency:
            raise ValueError(
                f"Refusing to combine {self.currency} with {other.currency}; "
                "cross-currency arithmetic must be explicit."
            )

    def __add__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        return Money(paise=self.paise + other.paise, currency=self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        return Money(paise=self.paise - other.paise, currency=self.currency)

    def __neg__(self) -> "Money":
        return Money(paise=-self.paise, currency=self.currency)

    def __mul__(self, factor: Union[int, Decimal]) -> "Money":
        if isinstance(factor, float):  # pragma: no cover - defensive
            raise TypeError("Money * float is forbidden; use Decimal.")
        if isinstance(factor, int):
            return Money(paise=self.paise * factor, currency=self.currency)
        scaled = (Decimal(self.paise) * factor).quantize(Decimal(1), rounding=ROUNDING)
        return Money(paise=int(scaled), currency=self.currency)

    __rmul__ = __mul__

    def ratio_to(self, other: "Money") -> Decimal:
        """Exact ratio self/other. Returns Decimal(0) when `other` is zero."""
        self._assert_same_currency(other)
        if other.paise == 0:
            return Decimal(0)
        return Decimal(self.paise) / Decimal(other.paise)

    def clamp_at_least_zero(self) -> "Money":
        return self if self.paise >= 0 else Money.zero(self.currency)

    # -------------------------------------------------------------- ordering

    def __lt__(self, other: "Money") -> bool:
        self._assert_same_currency(other)
        return self.paise < other.paise

    def __le__(self, other: "Money") -> bool:
        self._assert_same_currency(other)
        return self.paise <= other.paise

    def __gt__(self, other: "Money") -> bool:
        self._assert_same_currency(other)
        return self.paise > other.paise

    def __ge__(self, other: "Money") -> bool:
        self._assert_same_currency(other)
        return self.paise >= other.paise

    # -------------------------------------------------------------- rendering

    def __str__(self) -> str:
        sign = "-" if self.paise < 0 else ""
        whole, frac = divmod(abs(self.paise), PAISE_PER_RUPEE)
        return f"{sign}₹{whole:,}.{frac:02d}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Money({self})"

    @field_validator("paise")
    @classmethod
    def _reject_bool(cls, v: Any) -> int:
        if isinstance(v, bool):  # bool is an int subclass; almost always a bug
            raise ValueError("paise must be an int, not a bool")
        return v


def money_sum(items: Iterable[Money], currency: Currency = Currency.INR) -> Money:
    """Sum an iterable of Money exactly. Empty iterable -> zero."""
    total = Money.zero(currency)
    for item in items:
        total = total + item
    return total
