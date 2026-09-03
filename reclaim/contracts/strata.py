"""Stratification keys for the randomised batch experiment.

§12.1: "Stratified by amount band x failure class x segment."

This lives outside ``case.py`` so that the metrics module and the arm assigner can
import it without pulling in the action catalog and policy engine.

CONTRACT DECISION (JC-02, continued)
------------------------------------
``StratumKey`` is **frozen at case creation and stored**. If a decline code is
later re-mapped, or the AFA threshold moves in config, the stored stratum does not
change. Two reasons:

1. Arm assignment must be immutable (§12.1). If the stratum were recomputed, a
   stratum-keyed estimator would silently re-weight completed cases.
2. A judge reproducing the run must get the same strata from the same seed.

The consequence -- a stratum can be "wrong" relative to a corrected taxonomy -- is
accepted and reported. Phase 1 stores ``stratum_definition_version`` alongside.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.enums import AmountBand, RiskClass, Segment
from reclaim.contracts.money import Money

__all__ = [
    "AMOUNT_BAND_UPPER_BOUNDS_INR",
    "STRATUM_DEFINITION_VERSION",
    "StratumKey",
    "amount_band",
    "legal_failure_classes",
    "legal_failure_classes_for",
]

STRATUM_DEFINITION_VERSION = "1.0.0"

#: Inclusive upper bounds in rupees. ``None`` means unbounded.
#: Boundaries mirror policy thresholds but are frozen independently of config.
AMOUNT_BAND_UPPER_BOUNDS_INR: tuple[tuple[AmountBand, Decimal | None], ...] = (
    (AmountBand.LE_2K, Decimal("2000")),        # T0 auto-reschedule ceiling (§14.2)
    (AmountBand.LE_15K, Decimal("15000")),      # AFA-every-time threshold (§14.1)
    (AmountBand.LE_1L, Decimal("100000")),      # category relaxation flagged in §11.2
    (AmountBand.LE_10L, Decimal("1000000")),
    (AmountBand.GT_10L, None),
)


def amount_band(amount: Money) -> AmountBand:
    """Map an amount to its frozen stratification band.

    Negative amounts are treated as their absolute value; a negative at-risk
    amount is a data bug, not a band.
    """
    rupees = abs(amount.rupees)
    for band, upper in AMOUNT_BAND_UPPER_BOUNDS_INR:
        if upper is None or rupees <= upper:
            return band
    return AmountBand.GT_10L  # pragma: no cover - unreachable by construction


def legal_failure_classes() -> frozenset[str]:
    """The **flat union** of vocabularies permitted in ``StratumKey.failure_class``.

    Deliberately unscoped: a ``StratumKey`` is built and validated without knowing
    which risk class produced it (the metrics module and the arm assigner hold
    strata that are not attached to a case), so this is the only check that key can
    make on its own. It answers "is this a word in either vocabulary", not "is this
    word legal for *that* case".

    The per-risk-class scoping is ``legal_failure_classes_for`` below, and it is
    ``RiskCase`` -- which does know its risk class -- that applies it (Q10).
    """
    return frozenset({c.value for c in DeclineClass} | {r.value for r in RiskClass})


def legal_failure_classes_for(risk_class: RiskClass) -> frozenset[str]:
    """The failure-class vocabulary legal for a case of ``risk_class`` (Q10).

    ``FAILED_RECURRING_DEBIT`` (D1) is the one detector that observes a PSP decline
    code, so a D1 case may stratify on the normalised ``DeclineClass`` -- which is
    the design intent of ``StratumKey.failure_class`` carrying two vocabularies at
    all. ``RiskClass.FAILED_RECURRING_DEBIT.value`` stays legal for a D1 case whose
    decline code has not been normalised yet; a stratum is frozen at creation
    (JC-23/JC-02) and cannot wait for the taxonomy lookup.

    **Every other risk class is restricted to its own ``RiskClass`` value**, D2
    included. D2 (``PREDICTED_TO_FAIL_DEBIT``) predicts a failure that has not
    happened, so the decline class it would stratify on does not exist at detection
    time; whether a *predicted* class belongs in the stratum is an open question
    that the D2 detector must answer, not something to guess here. See CONTRACTS.md
    Q10, "What this does not cover".
    """
    if risk_class is RiskClass.FAILED_RECURRING_DEBIT:
        return frozenset({c.value for c in DeclineClass} | {risk_class.value})
    return frozenset({risk_class.value})


class StratumKey(BaseModel):
    """amount band x failure class x segment. Hashable, used as a dict key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_band: AmountBand
    failure_class: str = Field(
        description="A DeclineClass value (D1 cases, once the code is normalised) "
        "or a RiskClass value. Validated here against the flat union of both "
        "vocabularies; RiskCase applies the per-risk-class scoping (Q10)."
    )
    segment: Segment

    @field_validator("failure_class")
    @classmethod
    def _known_vocabulary(cls, v: str) -> str:
        if v not in legal_failure_classes():
            raise ValueError(
                f"failure_class {v!r} is not a DeclineClass or RiskClass value"
            )
        return v

    @property
    def key(self) -> str:
        """Stable canonical string. Used in reports and as a database key."""
        return f"{self.amount_band.value}|{self.failure_class}|{self.segment.value}"

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.key

    @classmethod
    def build(
        cls,
        *,
        amount: Money,
        failure_class: DeclineClass | RiskClass | str,
        segment: Segment,
    ) -> "StratumKey":
        raw = failure_class.value if hasattr(failure_class, "value") else str(failure_class)
        return cls(amount_band=amount_band(amount), failure_class=raw, segment=segment)
