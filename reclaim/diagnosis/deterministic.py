"""Deterministic fallback diagnostician -- no LLM, no tool loop, no investigation.

What this is
------------
Given an already-opened ``RiskCase``, return a schema-valid ``Diagnosis`` by
table lookup alone:

* ``canonical_decline_class`` present (a normalised D1 case) -> dispatch on it via
  the frozen ``DECLINE_CLASS_META`` taxonomy;
* absent (D3+, or a D1 whose PSP code was never normalised) -> fall back to
  ``risk_class`` and a small explicit table.

This is the branch §9.1 labels "LLM fail / timeout -> PLANNED (via deterministic
fallback)", and it is proven here in isolation so real reasoning can be layered on
top later without the safety net moving.

Deliberate limits (Phase 1 replaces or builds on top)
-----------------------------------------------------
* **No disambiguation, and the confidence says so.** A dispatch the frozen
  taxonomy records with more than one candidate root cause -- e.g.
  ``PAYER_AUTHORIZATION_MISSING_AMBIGUOUS`` (H4 AFA-incomplete / H3 mandate-dead
  / H5 churn-intent), the plan's bimodal case (§9.2) -- takes the first candidate
  (H4, whose action family, re-notify + one-tap AFA link, is the lowest blast
  radius), files the rest as ``alternative_root_causes``, and is reported at
  ``CONTESTED_CAUSE_CONFIDENCE`` -- below the policy engine's actionable floor, so
  it always tiers up to a human. Choosing *between* the candidates from evidence
  is exactly what the LLM diagnosis adds. The contested rung keys off the
  *presence of alternatives*, not the class name, so it generalises.
* **Never asserts H6 (our-side systemic).** ``Diagnosis`` requires an incident or
  cohort id whenever ``root_cause`` is H6, and a table lookup has neither. A
  false systemic-suppression call is the most damaging wrong diagnosis in the
  system (§10.1), so any class whose primary candidate is H6 abstains
  (``root_cause=UNKNOWN``) and routes to a human / the real diagnosis path.
* **Fixed confidence, three rungs, no calibration.** The path gathered no
  discriminating evidence, so it must not present as a calibrated finding (§12.3
  is about the LLM path): ``KNOWN_CAUSE_CONFIDENCE`` for a clean single-candidate
  dispatch, ``CONTESTED_CAUSE_CONFIDENCE`` (tiers up) when the taxonomy files
  alternatives, ``ABSTENTION_CONFIDENCE`` for ``UNKNOWN``.
* **Synthetic evidence when the caller supplies none.** ``Diagnosis`` requires at
  least one cited ``Claim``. On bare seed data there is no upstream
  ``CanonicalEvent`` to cite, so a single self-reference to the case's own
  detection is synthesised (``source_system="reclaim.deterministic_fallback"``
  makes that explicit). Phase 1's detector should pass the real triggering
  ``CanonicalEvent.ingest_key`` via ``evidence=`` instead.

Purity
------
No clock, no I/O. The caller passes ``created_at``; arm A3's scheduler owns the
clock, this does not.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final, Mapping, Sequence

from reclaim.contracts.case import (
    ABSTENTION_CONFIDENCE_CEILING,
    Claim,
    Diagnosis,
    EvidenceRef,
    RiskCase,
)
from reclaim.contracts.decline_taxonomy import DECLINE_CLASS_META, DeclineClass
from reclaim.contracts.enums import DiagnosisSource, RiskClass, RootCauseClass
from reclaim.contracts.events import EventType
from reclaim.contracts.policy_format import PolicyThresholds

__all__ = [
    "ABSTENTION_CONFIDENCE",
    "CONTESTED_CAUSE_CONFIDENCE",
    "KNOWN_CAUSE_CONFIDENCE",
    "RISK_CLASS_FALLBACK_ROOT_CAUSE",
    "diagnose",
]

# ---------------------------------------------------------------------------
# Confidence -- a three-rung ladder pinned against the frozen contracts
# ---------------------------------------------------------------------------

#: Confidence for a *clean* deterministic dispatch: a known root cause the frozen
#: taxonomy records with a single candidate. Strictly above
#: ``ABSTENTION_CONFIDENCE_CEILING`` so a known cause is never mistaken for an
#: abstention, but deliberately no higher than "a considered default": this path
#: did not investigate. Whether 0.6 clears
#: ``PolicyThresholds.diagnosis_confidence_floor`` (0.55 today) and is therefore
#: nominally actionable is left to the policy engine -- and only the
#: low-blast-radius single-cause classes this path can resolve (H1/H2/H3) reach
#: it anyway; the engine still composes confidence with amount and reversibility
#: (§14.2).
KNOWN_CAUSE_CONFIDENCE: Final[Decimal] = Decimal("0.6")

#: Confidence for a *contested* dispatch: a known primary root cause where the
#: frozen taxonomy also files ``alternative_root_causes`` that demand different
#: actions -- the plan's bimodal case (§9.2), e.g.
#: ``PAYER_AUTHORIZATION_MISSING_AMBIGUOUS`` resolving to H4 ("re-notify + AFA
#: link") vs H5 ("retention path, never a payment nudge"). Deliberately below the
#: policy engine's actionable-confidence floor so a contested deterministic
#: diagnosis *always* tiers up to a human rather than auto-acting on a coin flip
#: between opposite interventions -- and still above the abstention ceiling,
#: because the path does have a defensible primary hypothesis. Selected on the
#: *shape* of the dispatch (non-empty alternatives), never the class name, so it
#: generalises if the taxonomy ever files another class as contested.
CONTESTED_CAUSE_CONFIDENCE: Final[Decimal] = Decimal("0.52")

#: Confidence for a deterministic abstention (``root_cause=UNKNOWN``). Must be
#: ``<= ABSTENTION_CONFIDENCE_CEILING`` or ``Diagnosis`` rejects it; set well
#: below so it reads as genuine "no idea", not "borderline".
ABSTENTION_CONFIDENCE: Final[Decimal] = Decimal("0.3")

#: The policy engine's actionable-confidence floor (§14.2, JC-22) -- a tunable
#: that lives in YAML config. This is its *declared default*, read without
#: constructing the model. The runtime value may differ; pinning the contested
#: rung against the default means a lower config value is a deliberate choice,
#: not a silent regression that lets contested diagnoses auto-act.
_DIAGNOSIS_CONFIDENCE_FLOOR: Final[Decimal] = Decimal(
    PolicyThresholds.model_fields["diagnosis_confidence_floor"].default
)

# The confidence ladder, tied to the frozen contracts so a change there breaks
# the build here rather than drifting silently:
#     ABSTENTION_CONFIDENCE <= ceiling < CONTESTED < floor ,  CONTESTED < KNOWN
assert ABSTENTION_CONFIDENCE <= ABSTENTION_CONFIDENCE_CEILING, (
    "abstention confidence must not exceed the frozen ceiling"
)
assert ABSTENTION_CONFIDENCE_CEILING < CONTESTED_CAUSE_CONFIDENCE, (
    "a contested dispatch still names a primary cause; keep it above the "
    "abstention ceiling"
)
assert CONTESTED_CAUSE_CONFIDENCE < _DIAGNOSIS_CONFIDENCE_FLOOR, (
    "a contested dispatch must sit below the policy confidence floor so it "
    "always tiers up to a human"
)
assert CONTESTED_CAUSE_CONFIDENCE < KNOWN_CAUSE_CONFIDENCE, (
    "a contested dispatch must be less confident than a clean single-cause one"
)
assert KNOWN_CAUSE_CONFIDENCE > ABSTENTION_CONFIDENCE_CEILING, (
    "a known cause must be distinguishable from an abstention by confidence alone"
)


# ---------------------------------------------------------------------------
# Risk-class fallback (used only when there is no observed decline code)
# ---------------------------------------------------------------------------

#: Root cause for a case with no normalised decline class, by risk class. Only
#: ``OVERDUE_RECEIVABLE`` gets a non-abstention default: a bare overdue B2B
#: receivable, with no document analysis or payment history, has H9 (liquidity /
#: willful delay) as its safe generic -- the action family is a payment plan and
#: an escalation ladder, both human-gated (§9.2 H9). Every other risk class -- a
#: predicted failure, a checkout abandonment, a systemic anomaly, silent leakage,
#: or a D1 whose code was never normalised -- has no defensible single default
#: from a table alone and abstains.
RISK_CLASS_FALLBACK_ROOT_CAUSE: Mapping[RiskClass, RootCauseClass] = {
    RiskClass.OVERDUE_RECEIVABLE: RootCauseClass.H9_B2B_LIQUIDITY_OR_WILLFUL_DELAY,
}

_MAX_ALTERNATIVES: Final[int] = 3  # mirrors Diagnosis.alternative_root_causes cap

_ABSTAIN: Final[RootCauseClass] = RootCauseClass.UNKNOWN
_H6: Final[RootCauseClass] = RootCauseClass.H6_OUR_SIDE_SYSTEMIC


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch_decline_class(
    decline_class: DeclineClass,
) -> tuple[RootCauseClass, tuple[RootCauseClass, ...]]:
    """(root_cause, alternatives) from the frozen taxonomy. Abstains on H6."""
    candidates = DECLINE_CLASS_META[decline_class].candidate_root_causes
    primary = candidates[0] if candidates else _ABSTAIN
    if primary in (_H6, _ABSTAIN):
        return _ABSTAIN, ()
    alternatives: list[RootCauseClass] = []
    for cause in candidates[1:]:
        if cause in (primary, _H6, _ABSTAIN) or cause in alternatives:
            continue
        alternatives.append(cause)
    return primary, tuple(alternatives[:_MAX_ALTERNATIVES])


def _dispatch_risk_class(
    risk_class: RiskClass,
) -> tuple[RootCauseClass, tuple[RootCauseClass, ...]]:
    primary = RISK_CLASS_FALLBACK_ROOT_CAUSE.get(risk_class, _ABSTAIN)
    if primary is RootCauseClass.H9_B2B_LIQUIDITY_OR_WILLFUL_DELAY:
        # H8 (process defect -> fix and resend the invoice) is a materially
        # different action from H9 (chase payment), and a table-only D3 diagnosis
        # cannot tell them apart. Filing H8 therefore also routes D3 through the
        # contested rung in ``diagnose``: it tiers up, not auto-escalates.
        return primary, (RootCauseClass.H8_B2B_PROCESS_DEFECT,)
    return primary, ()


# ---------------------------------------------------------------------------
# Diagnosis assembly
# ---------------------------------------------------------------------------


def _id_body(prefixed_id: str) -> str:
    """The part of a ``pfx_body`` id after the first underscore, or the whole
    string if there is none. Valid contract ids always have the underscore."""
    return prefixed_id.split("_", 1)[-1] if "_" in prefixed_id else prefixed_id


def _derive_diagnosis_id(case_id: str) -> str:
    return f"dx_{_id_body(case_id)}_det"


def _synthetic_evidence_ref(case: RiskCase) -> EvidenceRef:
    """The one cited claim a deterministic diagnosis carries when the caller
    supplies no real event refs. Points at the case's own detection, not an
    ingested upstream event -- ``source_system`` says so."""
    is_d1 = case.risk_class is RiskClass.FAILED_RECURRING_DEBIT
    summary = f"{case.risk_class.value} detected on obligation {case.obligation_id}"
    if case.canonical_decline_class is not None:
        summary += f"; decline class {case.canonical_decline_class.value}"
    return EvidenceRef(
        evidence_id=f"ev_{_id_body(case.case_id)}_det",
        source_system="reclaim.deterministic_fallback",
        source_event_id=case.case_id,
        event_type=EventType.PAYMENT_ATTEMPT if is_d1 else EventType.OBLIGATION_UPDATED,
        observed_at=case.detected_at,
        summary=summary[:240],
    )


def _claim_statement(basis: str, root_cause: RootCauseClass, contested: bool) -> str:
    if root_cause is _ABSTAIN:
        text = (
            f"Deterministic fallback could not resolve a root cause from {basis}; "
            "abstaining (routes to a human / the LLM diagnosis path)."
        )
    elif contested:
        text = (
            f"Deterministic fallback mapped {basis} to primary root cause "
            f"{root_cause.value} via the frozen taxonomy, which also files "
            "alternatives demanding different actions; reported below the policy "
            "confidence floor so a human confirms the choice."
        )
    else:
        text = (
            f"Deterministic fallback mapped {basis} to root cause "
            f"{root_cause.value} via the frozen taxonomy; no evidence was gathered."
        )
    return text[:400]


def _reasoning_summary(basis: str, root_cause: RootCauseClass, contested: bool) -> str:
    if root_cause is _ABSTAIN:
        text = (
            f"Deterministic fallback (no LLM, no investigation). {basis}. No single "
            "root cause is defensible from a table lookup alone; abstained."
        )
    elif contested:
        text = (
            f"Deterministic fallback (no LLM, no investigation). Dispatched on "
            f"{basis}; primary root cause {root_cause.value}. The frozen taxonomy "
            "files alternatives that demand different actions (§9.2's bimodal "
            "case), so this is reported at tier-up confidence for a human to "
            "confirm; picking between them from evidence is the LLM diagnosis "
            "path's job."
        )
    else:
        text = (
            f"Deterministic fallback (no LLM, no investigation). Dispatched on "
            f"{basis}; single-candidate root cause {root_cause.value} per the "
            "frozen taxonomy."
        )
    return text[:1200]


def diagnose(
    case: RiskCase,
    *,
    created_at: datetime,
    evidence: Sequence[EvidenceRef] | None = None,
    diagnosis_id: str | None = None,
) -> Diagnosis:
    """Produce a deterministic-fallback ``Diagnosis`` for ``case``.

    ``evidence`` -- real ``EvidenceRef``s from the triggering event(s), if the
    caller has them; otherwise a single self-reference to the case's detection is
    synthesised so the ``Diagnosis`` schema's "every claim carries evidence" rule
    is satisfied.

    ``diagnosis_id`` -- defaults to a value derived from ``case.case_id`` so two
    calls on the same case agree.
    """
    if case.canonical_decline_class is not None:
        root_cause, alternatives = _dispatch_decline_class(case.canonical_decline_class)
        basis = f"canonical_decline_class={case.canonical_decline_class.value}"
    else:
        root_cause, alternatives = _dispatch_risk_class(case.risk_class)
        basis = f"risk_class={case.risk_class.value} (no observed decline code)"

    # A dispatch is "contested" when the frozen taxonomy files alternatives that
    # demand different actions (§9.2's bimodal case). Keyed off the shape of the
    # result, never a class name, so it generalises to any future contested entry.
    contested = bool(alternatives)
    if root_cause is _ABSTAIN:
        confidence = ABSTENTION_CONFIDENCE
    elif contested:
        confidence = CONTESTED_CAUSE_CONFIDENCE
    else:
        confidence = KNOWN_CAUSE_CONFIDENCE

    refs = tuple(evidence) if evidence is not None else (_synthetic_evidence_ref(case),)
    claim = Claim(
        statement=_claim_statement(basis, root_cause, contested), evidence=refs
    )

    return Diagnosis(
        diagnosis_id=diagnosis_id or _derive_diagnosis_id(case.case_id),
        case_id=case.case_id,
        root_cause=root_cause,
        confidence=confidence,
        source=DiagnosisSource.DETERMINISTIC_FALLBACK,
        claims=(claim,),
        reasoning_summary=_reasoning_summary(basis, root_cause, contested),
        alternative_root_causes=alternatives,
        created_at=created_at,
    )
