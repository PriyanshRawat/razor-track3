"""Numeric units for statistics, probabilities and ratios.

Money is integer paise (``reclaim.contracts.money``). Everything else numeric that
crosses a module boundary or reaches the audit chain lives here.

CONTRACT DECISION (JC-15)
-------------------------
``reclaim.contracts.canonical`` **rejects floats**, because a float has no
canonical decimal form and the hash chain must be reproducible. But diagnosis
confidence, calibration curves, recovery rates and p-values are all naturally
floats. The resolution: every such number is a ``Decimal`` **quantised to a fixed
scale at the schema boundary**, so that

* ``0.81`` and ``0.8100`` produce byte-identical canonical JSON, and
* a model that emits ``0.8123456789`` and one that emits ``0.81234567891``
  produce the *same* recorded confidence, which is honest -- we do not have
  eleven significant figures of calibration.

Scales are deliberately small. Six decimal places on a probability is already
finer than any calibration we can demonstrate; nine on a p-value is enough for a
Benjamini-Hochberg cut at 1e-9.

Internal computation (bootstrap resampling, model inference, EWMA) uses ordinary
floats. Conversion happens once, at the point the number becomes a *recorded*
fact.

Fixed-point on the wire
-----------------------
``str(Decimal("1E-9"))`` is ``"1E-9"``: CPython switches to scientific notation
once the adjusted exponent falls below -6. Relying on that would make the audit
chain's bytes depend on an incidental formatting threshold, and would put
``"1E-9"`` in a row a human is meant to read. Every type here therefore carries an
explicit ``PlainSerializer`` that emits **fixed-point** text, so a p-value of 1e-9
is recorded as ``"0.000000001"`` at every scale we might later choose.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Final, Union

from pydantic import AfterValidator, BeforeValidator, Field, PlainSerializer

__all__ = [
    "MONEY_RATIO_SCALE",
    "PROBABILITY_SCALE",
    "PValue",
    "P_VALUE_SCALE",
    "Probability",
    "Ratio",
    "as_decimal",
    "fixed_point",
    "probability",
    "pvalue",
    "quantise",
    "ratio",
]

PROBABILITY_SCALE: Final[int] = 6
P_VALUE_SCALE: Final[int] = 9
MONEY_RATIO_SCALE: Final[int] = 6

_NumericInput = Union[int, float, str, Decimal]


def as_decimal(value: _NumericInput) -> Decimal:
    """Coerce to Decimal. Floats go via ``repr`` so 0.1 becomes 0.1, not
    0.1000000000000000055511151231257827."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def quantise(value: _NumericInput, scale: int) -> Decimal:
    """Round to ``scale`` decimal places, half-up, matching ``money.ROUNDING``."""
    exponent = Decimal(1).scaleb(-scale)
    return as_decimal(value).quantize(exponent, rounding=ROUND_HALF_UP)


def probability(value: _NumericInput) -> Decimal:
    """A probability in [0, 1], quantised to ``PROBABILITY_SCALE``."""
    return quantise(value, PROBABILITY_SCALE)


def pvalue(value: _NumericInput) -> Decimal:
    return quantise(value, P_VALUE_SCALE)


def ratio(value: _NumericInput) -> Decimal:
    """An unbounded non-negative ratio (recovery rate, cost per rupee, lift)."""
    return quantise(value, MONEY_RATIO_SCALE)


def fixed_point(value: Decimal) -> str:
    """Fixed-point text, never scientific notation.

    This is the wire and hash form of every quantised number in RECLAIM. It is a
    pure function of the (coefficient, exponent) pair that ``quantise`` fixes, so
    two numerically equal quantised values always produce the same bytes.
    """
    return format(value, "f")


#: A probability. Quantised on validation, so an unquantised Decimal handed to a
#: model is normalised rather than silently stored at the wrong scale.
Probability = Annotated[
    Decimal,
    BeforeValidator(as_decimal),
    Field(ge=Decimal(0), le=Decimal(1)),
    AfterValidator(probability),
    PlainSerializer(fixed_point, return_type=str, when_used="json"),
]

PValue = Annotated[
    Decimal,
    BeforeValidator(as_decimal),
    Field(ge=Decimal(0), le=Decimal(1)),
    AfterValidator(pvalue),
    PlainSerializer(fixed_point, return_type=str, when_used="json"),
]

#: A ratio that may exceed 1 (lift, cost per rupee recovered, contacts per recovery).
Ratio = Annotated[
    Decimal,
    BeforeValidator(as_decimal),
    AfterValidator(ratio),
    PlainSerializer(fixed_point, return_type=str, when_used="json"),
]
