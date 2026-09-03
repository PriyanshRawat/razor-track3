"""RiskCase, Diagnosis and Plan (deliverable #5, part 1).

These three are the working state of a single recovery attempt: what we found
(``RiskCase``), what we think caused it (``Diagnosis``), and what we intend to do
about it (``Plan``). The audit chain in ``reclaim.contracts.audit`` records how
each of them changed.

CONTRACT DECISION (JC-23): a case stores its stratum and arm, not just its inputs
--------------------------------------------------------------------------------
§12.1 requires arm assignment to be "logged at case creation, immutable". Both
fields are therefore ordinary stored fields on a frozen model rather than
properties recomputed from the amount and failure class. Recomputation would let a
later taxonomy correction silently re-stratify finished cases and re-weight the
estimator that reads them.

CONTRACT DECISION (JC-42): a D1 case names its decline class, and only D1 does
---------------------------------------------------------------------------------
``StratumKey.failure_class`` was built to carry two vocabularies -- a
``DeclineClass`` for cases that observe a PSP decline code, a ``RiskClass`` for
those that do not (``strata.py``). The original validator here required
``stratum.failure_class == risk_class.value`` for *every* case, which made the
first half of that design unreachable: no case could ever legally stratify on a
decline class, and the normalised class -- which ``events.py`` says is normalised
"against the taxonomy version recorded on the case" -- had nowhere on the case to
live.

The fix is scoped to **D1 (``FAILED_RECURRING_DEBIT``)**, because D1 is the only
detector whose input is a decline code. ``canonical_decline_class`` is populated
for D1 and must be ``None`` everywhere else, and the stratum may stratify on either
the risk class (code not yet normalised -- the stratum freezes at detection, JC-23,
and cannot wait) or on the decline class the case names.

**What this costs.** Two D1 cases with the same amount, segment and root cause can
now land in different strata depending on whether normalisation beat detection.
That is a real inconsistency in the weighting key, and the honest mitigation is
operational, not structural: Phase 1's detector must normalise before it opens the
case, so the risk-class fallback stays an escape hatch rather than a second regime.
Storing ``stratum_definition_version`` (JC-02) is what makes the split visible after
the fact.

**What it does not cover.** D2 (``PREDICTED_TO_FAIL_DEBIT``) is deliberately left
where it was -- restricted to its own risk class. A prediction has no observed
decline code, so the question of whether a *predicted* class belongs in a stratum
is the D2 detector's to answer. CONTRACTS.md Q10.

CONTRACT DECISION (JC-24): a claim without evidence cannot be constructed
------------------------------------------------------------------------
§9.2 ("every claim carries evidence") and the Recovery Receipt are the same
requirement seen from two ends. Rather than validate citations at render time --
when the LLM's answer is already in front of a reviewer -- ``Claim`` requires at
least one ``EvidenceRef`` at construction. An uncited claim is not a claim with a
missing field; it is not a claim.

``EvidenceRef`` points at ``(source_system, source_event_id)`` -- the ingest key of
``CanonicalEvent`` -- rather than quoting the event. A quote can drift from the
event; a key cannot. The ``summary`` is for the receipt, and is explicitly *not*
the evidence.

CONTRACT DECISION (JC-25): abstention is structural, not a threshold
--------------------------------------------------------------------
§12.3 measures abstention rate, and §14.2 says "low confidence tiers up". The
schema enforces the half of that which cannot be left to a policy rule: a
diagnosis of ``UNKNOWN`` may not carry high confidence. High confidence in "I do
not know" is not humility, it is a confidently wrong route into a plan.

The *numeric* floor that decides whether a known root cause is actionable stays in
``PolicyThresholds.diagnosis_confidence_floor``, because that is a tunable, and
tunables belong in config (JC-22).

CONTRACT DECISION (JC-26): a plan is a bounded, contiguous, single-case ladder
------------------------------------------------------------------------------
The planner is an LLM, so the ways a plan can be malformed are the ways a model
mis-generates: a dropped step, a step index reused, a step whose action refers to a
different case, a runaway list. Each is a validation error here. In particular the
cross-case check is a customer-safety property, not tidiness -- an action envelope
carrying a different ``case_id`` inside a plan would execute against the wrong
person while the audit row said otherwise.

``catalog_tier_floor`` is the strictest *base* tier of the steps' verbs. It is a
floor, not a decision: the policy engine may tier further up (never down), and the
executor asks the policy engine, not this property.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from reclaim.contracts.actions import ACTION_SPECS, ActionEnvelope
from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.enums import (
    ALLOWED_CASE_TRANSITIONS,
    Arm,
    AutonomyTier,
    CaseState,
    DiagnosisSource,
    PlanOrigin,
    RiskClass,
    RootCauseClass,
    Segment,
    StepTrigger,
    StopReason,
)
from reclaim.contracts.events import EventType
from reclaim.contracts.ids import (
    CaseId,
    CohortId,
    DiagnosisId,
    EvidenceId,
    IncidentId,
    ObligationId,
    PayerId,
    PlanId,
)
from reclaim.contracts.money import Money
from reclaim.contracts.strata import (
    StratumKey,
    amount_band,
    legal_failure_classes_for,
)
from reclaim.contracts.temporal import UtcDatetime
from reclaim.contracts.units import Probability
from reclaim.contracts.versions import CONTRACTS_SCHEMA_VERSION

__all__ = [
    "ABSTENTION_CONFIDENCE_CEILING",
    "Claim",
    "Diagnosis",
    "EvidenceRef",
    "MAX_CLAIMS_PER_DIAGNOSIS",
    "MAX_EVIDENCE_PER_CLAIM",
    "MAX_PLAN_STEPS",
    "Plan",
    "PlanStep",
    "RiskCase",
]

#: A plan longer than this is a model failure, not a strategy. Six steps covers
#: the longest ladder in §10.3 (notify -> retry -> message -> reauth link ->
#: escalate) with room to spare.
MAX_PLAN_STEPS = 8

MAX_EVIDENCE_PER_CLAIM = 12
MAX_CLAIMS_PER_DIAGNOSIS = 12

#: An UNKNOWN root cause above this is a contradiction (JC-25). Deliberately
#: generous: the point is to catch "unknown @ 0.95", not to tune abstention. The
#: tunable floor for *actionable* confidence lives in PolicyThresholds (JC-22).
ABSTENTION_CONFIDENCE_CEILING: Final[Decimal] = Decimal("0.5")


# ---------------------------------------------------------------------------
# Evidence and claims
# ---------------------------------------------------------------------------


class EvidenceRef(BaseModel):
    """A pointer at one ingested event.

    Not a quotation of it: the pair ``(source_system, source_event_id)`` is
    ``CanonicalEvent.ingest_key``, so a receipt can be re-derived from the event
    store rather than trusted from prose the model wrote.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: EvidenceId
    source_system: str = Field(min_length=1, max_length=64)
    source_event_id: str = Field(min_length=1, max_length=128)
    event_type: EventType
    observed_at: UtcDatetime = Field(
        description="The event's occurred_at, copied so a receipt can be rendered "
        "without a join. The event store remains the source of truth."
    )
    summary: str = Field(
        min_length=1,
        max_length=240,
        description="Human-readable gloss for the Recovery Receipt. Display only: "
        "the evidence is the referenced event, not this text.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ingest_key(self) -> str:
        return f"{self.source_system}:{self.source_event_id}"


class Claim(BaseModel):
    """One assertion, with the events that support it.

    ``evidence`` is ``min_length=1`` by contract (JC-24), not by convention.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=1, max_length=400)
    evidence: tuple[EvidenceRef, ...] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_PER_CLAIM,
        description="At least one. §9.2: every claim carries evidence.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence_count(self) -> int:
        return len(self.evidence)


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


class Diagnosis(BaseModel):
    """A root-cause hypothesis (H1-H9 or UNKNOWN) with calibrated confidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    diagnosis_id: DiagnosisId
    case_id: CaseId
    root_cause: RootCauseClass
    confidence: Probability = Field(
        description="Quantised to six decimals (JC-15) so the audit chain can hash "
        "it. Calibration is measured in §12.3; this field is what is measured."
    )
    source: DiagnosisSource
    claims: tuple[Claim, ...] = Field(
        min_length=1,
        max_length=MAX_CLAIMS_PER_DIAGNOSIS,
        description="At least one cited claim, whatever the source.",
    )
    reasoning_summary: str = Field(
        default="",
        max_length=1200,
        description="Prose for the receipt. Required of an LLM diagnosis, optional "
        "for the deterministic fallback, which reasons by table.",
    )
    alternative_root_causes: tuple[RootCauseClass, ...] = Field(
        default=(),
        max_length=3,
        description="Hypotheses considered and not chosen, for the receipt's "
        "'what else it could be' line.",
    )
    incident_id: IncidentId | None = Field(
        default=None,
        description="Set when the root cause is H6 (ours). Links the case to the "
        "systemic incident so suppression can be reasoned about.",
    )
    cohort_id: CohortId | None = None
    model_id: str | None = Field(
        default=None, description="Required when source is the LLM (§15 replay)."
    )
    prompt_version: str | None = None
    created_at: UtcDatetime
    schema_version: str = CONTRACTS_SCHEMA_VERSION

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_abstention(self) -> bool:
        """§12.3's abstention rate numerator."""
        return self.root_cause is RootCauseClass.UNKNOWN

    @model_validator(mode="after")
    def _unknown_may_not_be_confident(self) -> "Diagnosis":
        if self.root_cause is RootCauseClass.UNKNOWN and (
            self.confidence > ABSTENTION_CONFIDENCE_CEILING
        ):
            raise ValueError(
                f"root_cause=unknown with confidence {self.confidence} is a "
                "contradiction; abstention must be uncertain "
                f"(<= {ABSTENTION_CONFIDENCE_CEILING})"
            )
        return self

    @model_validator(mode="after")
    def _llm_diagnoses_explain_themselves(self) -> "Diagnosis":
        if self.source is DiagnosisSource.LLM and not self.reasoning_summary.strip():
            raise ValueError(
                "an LLM diagnosis must carry a reasoning_summary; the receipt has "
                "nowhere else to get one"
            )
        return self

    @model_validator(mode="after")
    def _alternatives_exclude_the_chosen_cause(self) -> "Diagnosis":
        if self.root_cause in self.alternative_root_causes:
            raise ValueError("alternative_root_causes may not repeat root_cause")
        if len(set(self.alternative_root_causes)) != len(self.alternative_root_causes):
            raise ValueError("alternative_root_causes contains duplicates")
        return self

    @model_validator(mode="after")
    def _our_side_diagnoses_name_the_incident(self) -> "Diagnosis":
        """H6 asserts the failure is ours. §10.1 routes that to suppression plus an
        incident, so a bare H6 with nothing to point at is not actionable."""
        if self.root_cause is RootCauseClass.H6_OUR_SIDE_SYSTEMIC and (
            self.incident_id is None and self.cohort_id is None
        ):
            raise ValueError(
                "an H6 (our-side systemic) diagnosis must reference an incident or a "
                "cohort; otherwise nothing can be suppressed or investigated"
            )
        return self


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class PlanStep(BaseModel):
    """One action plus the condition under which it fires.

    The trigger vocabulary is closed (``StepTrigger``): the planner selects a
    branch condition, it does not write one. A free-text condition would be
    unevaluable by the deterministic scheduler and unauditable afterwards.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_index: int = Field(ge=0, lt=MAX_PLAN_STEPS)
    trigger: StepTrigger
    action: ActionEnvelope
    earliest_at: UtcDatetime = Field(
        description="Not-before time. The policy engine may defer later (quiet "
        "hours, notification lead); it never fires earlier."
    )
    rationale: str = Field(default="", max_length=400)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def base_tier(self) -> AutonomyTier:
        return ACTION_SPECS[self.action.action.action_type].base_tier


class Plan(BaseModel):
    """An ordered ladder of conditional steps for one case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: PlanId
    case_id: CaseId
    diagnosis_id: DiagnosisId
    origin: PlanOrigin
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=MAX_PLAN_STEPS)
    created_at: UtcDatetime
    model_id: str | None = None
    prompt_version: str | None = None
    schema_version: str = CONTRACTS_SCHEMA_VERSION

    @computed_field  # type: ignore[prop-decorator]
    @property
    def step_count(self) -> int:
        return len(self.steps)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def catalog_tier_floor(self) -> AutonomyTier:
        """The strictest base tier among the steps' verbs.

        A floor only: the policy engine composes this with amount, reversibility
        and confidence, and may tier up. Nothing tiers down (§14.2).
        """
        return AutonomyTier.strictest(*(step.base_tier for step in self.steps))

    @model_validator(mode="after")
    def _indices_are_contiguous_from_zero(self) -> "Plan":
        indices = [step.step_index for step in self.steps]
        if indices != list(range(len(indices))):
            raise ValueError(
                f"plan step indices must be contiguous from 0, got {indices}; a gap "
                "means a step was dropped between the planner and here"
            )
        return self

    @model_validator(mode="after")
    def _first_step_cannot_depend_on_a_predecessor(self) -> "Plan":
        first = self.steps[0]
        if first.trigger is not StepTrigger.ALWAYS:
            raise ValueError(
                f"step 0 has trigger {first.trigger.value}, which references a "
                "previous step that does not exist; the first step must be 'always'"
            )
        return self

    @model_validator(mode="after")
    def _every_step_belongs_to_this_case(self) -> "Plan":
        for step in self.steps:
            if step.action.case_id != self.case_id:
                raise ValueError(
                    f"step {step.step_index} carries an action for case "
                    f"{step.action.case_id!r} but the plan is for {self.case_id!r}; "
                    "executing it would act on the wrong customer"
                )
        return self

    @model_validator(mode="after")
    def _steps_do_not_move_backwards_in_time(self) -> "Plan":
        previous: UtcDatetime | None = None
        for step in self.steps:
            if previous is not None and step.earliest_at < previous:
                raise ValueError(
                    f"step {step.step_index} is scheduled before its predecessor; a "
                    "ladder that runs backwards cannot be evaluated by its triggers"
                )
            previous = step.earliest_at
        return self

    @model_validator(mode="after")
    def _action_ids_are_unique(self) -> "Plan":
        ids = [step.action.action_id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError(
                "duplicate action_id within a plan; each step is a distinct act and "
                "needs its own audit row (invariant #8)"
            )
        return self


# ---------------------------------------------------------------------------
# Case
# ---------------------------------------------------------------------------


class RiskCase(BaseModel):
    """One obligation at risk, from detection to a terminal state.

    Frozen. State changes produce a new instance plus an audit row; there is no
    in-place mutation, so an unaudited state change is not expressible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: CaseId
    obligation_id: ObligationId
    payer_id: PayerId
    risk_class: RiskClass
    segment: Segment
    amount_at_risk: Money = Field(
        description="Recognised once, at detection (§13 anti-double-counting). "
        "Partial payments reduce the outstanding balance on the obligation; they "
        "do not rewrite this number."
    )
    detected_at: UtcDatetime
    canonical_decline_class: DeclineClass | None = Field(
        default=None,
        description="The normalised decline class behind a D1 case, or None. "
        "Populated only for FAILED_RECURRING_DEBIT: no other detector observes a "
        "PSP decline code (JC-42).",
    )
    stratum: StratumKey = Field(
        description="Frozen at creation (JC-23/JC-02). Never recomputed."
    )
    arm: Arm = Field(
        description="Assigned at creation from case_id + experiment salt, and "
        "immutable thereafter (§12.1)."
    )
    state: CaseState
    recovery_window_ends_at: UtcDatetime = Field(
        description="§13's fixed observation window. Recovery after this instant is "
        "not counted, so that arms are compared over equal horizons."
    )
    active_plan_id: PlanId | None = None
    active_diagnosis_id: DiagnosisId | None = None
    stop_reason: StopReason | None = None
    stopped_at: UtcDatetime | None = None
    incident_id: IncidentId | None = None
    cohort_id: CohortId | None = None
    experiment_id: str | None = Field(
        default=None,
        description="Which randomised batch this case belongs to. None for cases "
        "created outside an experiment run.",
    )
    schema_version: str = CONTRACTS_SCHEMA_VERSION

    # -- derived ----------------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def at_risk_recognised_at(self) -> UtcDatetime:
        """§13: at-risk is recognised at detection, once. Exposed as its own name
        so metric code never has to remember that it equals ``detected_at``."""
        return self.detected_at

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_terminal(self) -> bool:
        return not ALLOWED_CASE_TRANSITIONS[self.state]

    def can_transition_to(self, target: CaseState) -> bool:
        """Whether ``target`` is reachable from the current state (§9.4 table)."""
        return target in ALLOWED_CASE_TRANSITIONS[self.state]

    # -- validation -------------------------------------------------------

    @model_validator(mode="after")
    def _stop_reason_iff_stopped(self) -> "RiskCase":
        """``STOPPED`` and ``stop_reason`` imply each other.

        A stopped case with no reason is unreviewable; a live case carrying a stop
        reason is a stop that did not take effect. Both are silent failures, so
        both are validation errors. ``RECOVERED`` and ``WRITTEN_OFF`` are terminal
        without being stops and need no reason.
        """
        if self.state is CaseState.STOPPED and self.stop_reason is None:
            raise ValueError(
                "a stopped case must carry a stop_reason; an unexplained stop cannot "
                "be reviewed and cannot be counted in §12.4's stop-reason breakdown"
            )
        if self.state is not CaseState.STOPPED and self.stop_reason is not None:
            raise ValueError(
                f"state {self.state.value} carries stop_reason "
                f"{self.stop_reason.value}; only a stopped case may name one"
            )
        return self

    @model_validator(mode="after")
    def _stopped_at_accompanies_the_stop(self) -> "RiskCase":
        if (self.stopped_at is None) != (self.stop_reason is None):
            raise ValueError("stopped_at and stop_reason must be set together")
        if self.stopped_at is not None and self.stopped_at < self.detected_at:
            raise ValueError("stopped_at precedes detected_at")
        return self

    @model_validator(mode="after")
    def _window_closes_after_detection(self) -> "RiskCase":
        if self.recovery_window_ends_at <= self.detected_at:
            raise ValueError(
                "recovery_window_ends_at must be after detected_at; a non-positive "
                "window makes every arm's recovery rate zero"
            )
        return self

    @model_validator(mode="after")
    def _at_risk_is_positive(self) -> "RiskCase":
        if not self.amount_at_risk.is_positive:
            raise ValueError(
                "amount_at_risk must be positive; a zero or negative case is not at "
                "risk and would distort the §13 denominator"
            )
        return self

    @model_validator(mode="after")
    def _the_control_arm_does_nothing(self) -> "RiskCase":
        """A0 is the natural-recovery floor (§12.2). A plan attached to an A0 case
        would contaminate the control and overstate every arm's lift."""
        if self.arm is Arm.A0 and self.active_plan_id is not None:
            raise ValueError(
                "arm A0 is the no-action control; a case in it may not hold a plan"
            )
        return self

    @model_validator(mode="after")
    def _stratum_agrees_with_the_case(self) -> "RiskCase":
        """The stratum is stored, not derived (JC-23) -- but it must have been
        derived from *this* case when it was stored. A mismatch means the case was
        assembled by hand or migrated wrongly, and its arm assignment is suspect.

        All three axes are checked, not just the segment. The stratum is the
        weighting key for §12.1's headline estimate, so a case sitting in the wrong
        band is not a tagging error: it moves rupees between the buckets the
        incremental-recovery number is summed over."""
        if self.stratum.segment is not self.segment:
            raise ValueError(
                f"stratum segment {self.stratum.segment.value} disagrees with case "
                f"segment {self.segment.value}"
            )
        derived_band = amount_band(self.amount_at_risk)
        if self.stratum.amount_band is not derived_band:
            raise ValueError(
                f"stratum amount_band {self.stratum.amount_band.value} disagrees "
                f"with the band derived from amount_at_risk "
                f"({derived_band.value}); the stratum was not built from this case"
            )
        legal = legal_failure_classes_for(self.risk_class)
        if self.stratum.failure_class not in legal:
            raise ValueError(
                f"stratum failure_class {self.stratum.failure_class!r} is not legal "
                f"for a case of risk_class {self.risk_class.value!r}; legal values "
                f"are {sorted(legal)}"
            )
        return self

    @model_validator(mode="after")
    def _the_decline_class_belongs_to_a_failed_debit(self) -> "RiskCase":
        """Only D1 observes a decline code, and a stratum may not stratify on a
        class the case does not name (JC-42).

        The second half is the load-bearing one. ``failure_class`` carries two
        vocabularies, so a stratum reading ``insufficient_funds`` on a case whose
        only recorded failure is ``failed_recurring_debit`` is a bucket nobody can
        trace back to an observation -- and the buckets are what §12.1's headline is
        summed over.

        The converse is deliberately allowed: a case may name a decline class while
        its stratum still stratifies on the risk class. The stratum is frozen at
        detection (JC-23) and normalisation can land after it, so forbidding that
        would force a choice between a stale stratum and an unrecorded class."""
        if self.risk_class is not RiskClass.FAILED_RECURRING_DEBIT:
            if self.canonical_decline_class is not None:
                raise ValueError(
                    f"risk_class {self.risk_class.value} carries "
                    f"canonical_decline_class "
                    f"{self.canonical_decline_class.value!r}; only a failed "
                    "recurring debit observes a PSP decline code"
                )
            return self

        stratified_on_a_decline_class = (
            self.stratum.failure_class != self.risk_class.value
        )
        if stratified_on_a_decline_class:
            if self.canonical_decline_class is None:
                raise ValueError(
                    f"stratum failure_class "
                    f"{self.stratum.failure_class!r} is a decline class, but the "
                    "case does not record one; set canonical_decline_class so the "
                    "stratum can be traced to the observation it came from"
                )
            if self.canonical_decline_class.value != self.stratum.failure_class:
                raise ValueError(
                    f"canonical_decline_class "
                    f"{self.canonical_decline_class.value!r} disagrees with stratum "
                    f"failure_class {self.stratum.failure_class!r}; the case would "
                    "be weighted into a bucket it does not belong to"
                )
        return self
