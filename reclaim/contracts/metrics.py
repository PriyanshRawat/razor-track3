"""Business metric definitions (deliverable #6), matching HACKATHON_PLAN.md §13.

Every metric named in §13's table is defined exactly once, here, as a spec plus a
function. Phase 1 code calls these; nothing recomputes a metric inline. That is the
whole design: a finance judge asking "what exactly is net incremental recovery?"
gets one answer, and the scoreboard cannot quietly diverge from the slide.

CONTRACT DECISION (JC-32): specs and formulas ship together
------------------------------------------------------------
``METRIC_SPECS`` carries each metric's plan row, prose formula, unit, direction and
whether it is a headline or a guardrail. An import-time guard asserts that the set
of ``plan_row`` values equals ``PLAN_SECTION_13_ROWS`` -- so "matches §13 exactly"
is a test failure when it stops being true, not a claim in a README.

CONTRACT DECISION (JC-33): a rate with no denominator is None, not zero
-----------------------------------------------------------------------
Zero is an assertion ("we recovered nothing"); ``None`` is the absence of one ("no
cases here"). Returning 0.0 for an empty stratum would pull a weighted average
toward zero with no data behind it, which is exactly how a small-N arm gets
reported as a regression. Every rate function returns ``Decimal | None``.

CONTRACT DECISION (JC-34): rates are fixed-scale Decimals
----------------------------------------------------------
Same reason as JC-15: these numbers reach the audit chain and the published
scoreboard, and ``canonical_json`` rejects floats. Bootstrap resampling in Phase 1
may work in floats internally; it converts once, here, at the boundary where a
number becomes a *reported* fact.

CONTRACT DECISION (JC-35): the estimator weights by at-risk money, not case count
---------------------------------------------------------------------------------
§12.1 says "stratum-weighted"; it does not say weighted by what. Money is the
choice, because the headline is denominated in rupees and the plan defines the
per-arm quantity as *net recovered per rupee at risk*. Case-count weighting would
let a large stratum of small obligations mask a regression on the few large ones.
**Flagged for review** in CONTRACTS.md: this is a genuine judgment call, and the
opposite choice is defensible for the recovery-*rate* metric (which is per
obligation by §13's own definition).

CONTRACT DECISION (JC-36): confidence intervals are declared here, computed later
----------------------------------------------------------------------------------
``IncrementalRecoveryEstimate`` has ``ci_low``/``ci_high``/``resamples`` fields that
this module leaves unset. Bootstrap resampling (10,000 draws, §12.1) is Phase 1
work and needs the case-level data this module does not take. The *shape* of the
reported number is frozen now so the scoreboard cannot later publish a point
estimate with no interval and call it the headline.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Final, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reclaim.contracts.enums import Arm
from reclaim.contracts.money import Money, money_sum
from reclaim.contracts.strata import StratumKey
from reclaim.contracts.units import PValue, Ratio, ratio
from reclaim.contracts.versions import METRICS_VERSION

try:  # pragma: no cover - 3.11+
    from enum import StrEnum
except ImportError:  # pragma: no cover
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


__all__ = [
    "ArmOutcome",
    "BOOTSTRAP_RESAMPLES",
    "CostBreakdown",
    "GuardrailCheck",
    "IncrementalRecoveryEstimate",
    "METRIC_SPECS",
    "MetricDirection",
    "MetricKey",
    "MetricSpec",
    "MetricUnit",
    "PLAN_SECTION_13_ROWS",
    "complaint_rate",
    "contacts_per_recovery",
    "cost_per_rupee_recovered",
    "escalation_rate",
    "false_action_rate",
    "guardrail_contract_holds",
    "human_minutes_per_100k_recovered",
    "metric_spec",
    "net_incremental_recovery",
    "net_recovered",
    "net_recovered_per_rupee_at_risk",
    "opt_out_rate",
    "promise_kept_rate",
    "recovery_rate",
    "stratum_weighted_incremental_recovery",
    "total_amount_at_risk",
]

#: §12.1: "Bootstrap 95% CI (10,000 resamples)". Fixed before the run.
BOOTSTRAP_RESAMPLES: Final[int] = 10_000

MINUTES_PER_HOUR: Final[int] = 60
RUPEES_PER_LAKH: Final[int] = 100_000

#: The §13 table's row labels, verbatim, in the plan's order. The import-time
#: guard below ties METRIC_SPECS to this list.
PLAN_SECTION_13_ROWS: Final[tuple[str, ...]] = (
    "Amount at risk",
    "Gross recovered",
    "Cost to collect",
    "Net recovered",
    "Net incremental recovery",
    "Recovery rate",
    "Days-to-cash / ΔDSO",
    "False-action rate",
    "Contacts per recovery",
    "Human minutes per ₹100k recovered",
    "Cost per ₹ recovered",
    "Promise-kept rate",
    "Escalation rate",
    "Policy violations",
    "Opt-out & complaint rate",
    "Retained subscriptions",
)


class MetricKey(StrEnum):
    """One member per computed quantity."""

    AMOUNT_AT_RISK = "amount_at_risk"
    GROSS_RECOVERED = "gross_recovered"
    COST_TO_COLLECT = "cost_to_collect"
    NET_RECOVERED = "net_recovered"
    NET_INCREMENTAL_RECOVERY = "net_incremental_recovery"
    RECOVERY_RATE = "recovery_rate"
    DAYS_TO_CASH = "days_to_cash"
    FALSE_ACTION_RATE = "false_action_rate"
    CONTACTS_PER_RECOVERY = "contacts_per_recovery"
    HUMAN_MINUTES_PER_100K = "human_minutes_per_100k_recovered"
    COST_PER_RUPEE_RECOVERED = "cost_per_rupee_recovered"
    PROMISE_KEPT_RATE = "promise_kept_rate"
    ESCALATION_RATE = "escalation_rate"
    POLICY_VIOLATIONS = "policy_violations"
    OPT_OUT_RATE = "opt_out_rate"
    COMPLAINT_RATE = "complaint_rate"
    RETAINED_SUBSCRIPTIONS = "retained_subscriptions"


class MetricUnit(StrEnum):
    MONEY = "money"
    RATE = "rate"                # a proportion in [0, 1]
    RATIO = "ratio"              # unbounded, may exceed 1
    COUNT = "count"
    DAYS = "days"
    MINUTES_PER_LAKH = "minutes_per_lakh"


class MetricDirection(StrEnum):
    """Which way is better. Used by the scoreboard, and by the guardrail check."""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    MUST_BE_ZERO = "must_be_zero"
    NEUTRAL = "neutral"          # reported, not optimised (e.g. days-to-cash delta)


class MetricSpec(BaseModel):
    """The definition of one metric, as a reviewer would want it written down."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: MetricKey
    plan_row: str = Field(description="The §13 table row this implements, verbatim.")
    formula: str = Field(min_length=1, description="The definition, in words.")
    unit: MetricUnit
    direction: MetricDirection
    is_headline: bool = False
    is_guardrail: bool = Field(
        default=False,
        description="§13: guardrails are reported *beside* recovery, never netted "
        "into it.",
    )
    caveat: str = Field(default="", max_length=400)


METRIC_SPECS: Mapping[MetricKey, MetricSpec] = {
    MetricKey.AMOUNT_AT_RISK: MetricSpec(
        key=MetricKey.AMOUNT_AT_RISK,
        plan_row="Amount at risk",
        formula="Recognised once per obligation, at detection. A systemic "
        "incident's at-risk equals the sum of its member cases; it is not "
        "additive on top of them.",
        unit=MetricUnit.MONEY,
        direction=MetricDirection.NEUTRAL,
        caveat="Anti-double-counting rule; stated in the audit log.",
    ),
    MetricKey.GROSS_RECOVERED: MetricSpec(
        key=MetricKey.GROSS_RECOVERED,
        plan_row="Gross recovered",
        formula="Cash settled against that specific obligation within the "
        "recovery window.",
        unit=MetricUnit.MONEY,
        direction=MetricDirection.HIGHER_IS_BETTER,
        caveat="Settled, not authorised. Money outside the window is not counted.",
    ),
    MetricKey.COST_TO_COLLECT: MetricSpec(
        key=MetricKey.COST_TO_COLLECT,
        plan_row="Cost to collect",
        formula="PSP fees + failed-attempt fees + channel cost + LLM/infra cost "
        "+ (human minutes x loaded rate).",
        unit=MetricUnit.MONEY,
        direction=MetricDirection.LOWER_IS_BETTER,
    ),
    MetricKey.NET_RECOVERED: MetricSpec(
        key=MetricKey.NET_RECOVERED,
        plan_row="Net recovered",
        formula="Gross recovered - cost to collect. May be negative.",
        unit=MetricUnit.MONEY,
        direction=MetricDirection.HIGHER_IS_BETTER,
    ),
    MetricKey.NET_INCREMENTAL_RECOVERY: MetricSpec(
        key=MetricKey.NET_INCREMENTAL_RECOVERY,
        plan_row="Net incremental recovery",
        formula="((net recovered / at risk) in treatment - (net recovered / at "
        "risk) in control) x total at risk, stratum-weighted, with a bootstrap "
        "95% CI over 10,000 resamples.",
        unit=MetricUnit.MONEY,
        direction=MetricDirection.HIGHER_IS_BETTER,
        is_headline=True,
        caveat="Causal only within the randomisation. Not a claim about any "
        "portfolio we did not randomise.",
    ),
    MetricKey.RECOVERY_RATE: MetricSpec(
        key=MetricKey.RECOVERY_RATE,
        plan_row="Recovery rate",
        formula="Recovered obligations / at-risk obligations. Counted per "
        "obligation, not per rupee.",
        unit=MetricUnit.RATE,
        direction=MetricDirection.HIGHER_IS_BETTER,
    ),
    MetricKey.DAYS_TO_CASH: MetricSpec(
        key=MetricKey.DAYS_TO_CASH,
        plan_row="Days-to-cash / ΔDSO",
        formula="Mean days from detection to settlement; reported as a delta "
        "between arms.",
        unit=MetricUnit.DAYS,
        direction=MetricDirection.NEUTRAL,
        caveat="Reported as an arm delta. The absolute level is a property of the "
        "environment, not of the agent.",
    ),
    MetricKey.FALSE_ACTION_RATE: MetricSpec(
        key=MetricKey.FALSE_ACTION_RATE,
        plan_row="False-action rate",
        formula="Contacts sent where the cause was systemic, the customer had "
        "already paid, a dispute was open, or churn intent was the true cause, "
        "divided by outbound contacts.",
        unit=MetricUnit.RATE,
        direction=MetricDirection.LOWER_IS_BETTER,
        is_guardrail=True,
        caveat="Priced into the EV function via an estimated goodwill cost.",
    ),
    MetricKey.CONTACTS_PER_RECOVERY: MetricSpec(
        key=MetricKey.CONTACTS_PER_RECOVERY,
        plan_row="Contacts per recovery",
        formula="Outbound touches / recovered case.",
        unit=MetricUnit.RATIO,
        direction=MetricDirection.LOWER_IS_BETTER,
        is_guardrail=True,
        caveat="Efficiency and politeness at once; lower is better on both.",
    ),
    MetricKey.HUMAN_MINUTES_PER_100K: MetricSpec(
        key=MetricKey.HUMAN_MINUTES_PER_100K,
        plan_row="Human minutes per ₹100k recovered",
        formula="Approval-queue time in minutes, per ₹100,000 gross recovered.",
        unit=MetricUnit.MINUTES_PER_LAKH,
        direction=MetricDirection.LOWER_IS_BETTER,
    ),
    MetricKey.COST_PER_RUPEE_RECOVERED: MetricSpec(
        key=MetricKey.COST_PER_RUPEE_RECOVERED,
        plan_row="Cost per ₹ recovered",
        formula="Total cost to collect / gross recovered.",
        unit=MetricUnit.RATIO,
        direction=MetricDirection.LOWER_IS_BETTER,
    ),
    MetricKey.PROMISE_KEPT_RATE: MetricSpec(
        key=MetricKey.PROMISE_KEPT_RATE,
        plan_row="Promise-kept rate",
        formula="Promises honoured on or before their date / promises made.",
        unit=MetricUnit.RATE,
        direction=MetricDirection.HIGHER_IS_BETTER,
    ),
    MetricKey.ESCALATION_RATE: MetricSpec(
        key=MetricKey.ESCALATION_RATE,
        plan_row="Escalation rate",
        formula="Cases reaching a human / total cases.",
        unit=MetricUnit.RATE,
        direction=MetricDirection.NEUTRAL,
        caveat="Not minimised: escalation is the safe default when confidence is "
        "low (§14.2). Read beside human minutes per ₹100k.",
    ),
    MetricKey.POLICY_VIOLATIONS: MetricSpec(
        key=MetricKey.POLICY_VIOLATIONS,
        plan_row="Policy violations",
        formula="Count of executed actions that a policy rule would have denied. "
        "Reported as a hard number, not a rate.",
        unit=MetricUnit.COUNT,
        direction=MetricDirection.MUST_BE_ZERO,
        is_guardrail=True,
        caveat="Must be 0. One violation is a failed run, not a worse score.",
    ),
    MetricKey.OPT_OUT_RATE: MetricSpec(
        key=MetricKey.OPT_OUT_RATE,
        plan_row="Opt-out & complaint rate",
        formula="Cases that opted out / cases contacted.",
        unit=MetricUnit.RATE,
        direction=MetricDirection.LOWER_IS_BETTER,
        is_guardrail=True,
        caveat="The guardrail contract: recovery gained while this rises above "
        "control is rejected.",
    ),
    MetricKey.COMPLAINT_RATE: MetricSpec(
        key=MetricKey.COMPLAINT_RATE,
        plan_row="Opt-out & complaint rate",
        formula="Cases producing a complaint proxy / cases contacted.",
        unit=MetricUnit.RATE,
        direction=MetricDirection.LOWER_IS_BETTER,
        is_guardrail=True,
        caveat="A proxy, not a real complaint channel. Directional only.",
    ),
    MetricKey.RETAINED_SUBSCRIPTIONS: MetricSpec(
        key=MetricKey.RETAINED_SUBSCRIPTIONS,
        plan_row="Retained subscriptions",
        formula="Subscriptions still active at the end of the recovery window, "
        "among cases whose obligation was a subscription instalment.",
        unit=MetricUnit.COUNT,
        direction=MetricDirection.HIGHER_IS_BETTER,
        caveat="Secondary, with an explicit survival-analysis caveat. We do not "
        "claim ARR saved.",
    ),
}


def _guard_specs_match_the_plan() -> None:
    missing = set(METRIC_SPECS) ^ set(MetricKey)
    if missing:
        raise RuntimeError(f"METRIC_SPECS and MetricKey disagree: {missing}")
    rows = {spec.plan_row for spec in METRIC_SPECS.values()}
    if rows != set(PLAN_SECTION_13_ROWS):
        raise RuntimeError(
            "METRIC_SPECS no longer covers §13 exactly. "
            f"missing rows: {set(PLAN_SECTION_13_ROWS) - rows}; "
            f"invented rows: {rows - set(PLAN_SECTION_13_ROWS)}"
        )
    for key, spec in METRIC_SPECS.items():
        if spec.key is not key:
            raise RuntimeError(f"METRIC_SPECS[{key}] holds spec for {spec.key}")


_guard_specs_match_the_plan()


def metric_spec(key: MetricKey) -> MetricSpec:
    return METRIC_SPECS[key]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class CostBreakdown(BaseModel):
    """§13's cost-to-collect, as its five named components.

    Kept itemised rather than as one total so the scoreboard can answer "where did
    the cost go?" and so an arm that trades PSP fees for human minutes is visible
    as such.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    psp_fees: Money = Money.zero()
    failed_attempt_fees: Money = Money.zero()
    channel_cost: Money = Money.zero()
    llm_and_infra_cost: Money = Money.zero()
    human_minutes: Decimal = Field(default=Decimal(0), ge=Decimal(0))
    human_loaded_rate_per_hour: Money = Money.zero()

    @property
    def human_cost(self) -> Money:
        """Minutes at the loaded hourly rate, rounded once at the paise boundary."""
        if self.human_minutes == 0 or self.human_loaded_rate_per_hour.is_zero:
            return Money.zero(self.human_loaded_rate_per_hour.currency)
        return self.human_loaded_rate_per_hour * (
            self.human_minutes / Decimal(MINUTES_PER_HOUR)
        )

    @property
    def total(self) -> Money:
        return money_sum(
            [
                self.psp_fees,
                self.failed_attempt_fees,
                self.channel_cost,
                self.llm_and_infra_cost,
                self.human_cost,
            ]
        )


class ArmOutcome(BaseModel):
    """Everything one arm produced within one stratum, over the recovery window.

    The unit of the experiment is the obligation-case (§12.1), so every count here
    is a case count except ``outbound_contacts``, which is a touch count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: Arm
    stratum: StratumKey
    at_risk_case_count: int = Field(ge=0)
    recovered_case_count: int = Field(ge=0)
    total_at_risk: Money
    gross_recovered: Money
    cost: CostBreakdown
    outbound_contacts: int = Field(default=0, ge=0)
    escalations: int = Field(default=0, ge=0)
    promises_made: int = Field(default=0, ge=0)
    promises_kept: int = Field(default=0, ge=0)
    opt_outs: int = Field(default=0, ge=0)
    complaints: int = Field(default=0, ge=0)
    policy_violations: int = Field(default=0, ge=0)
    false_actions: int = Field(default=0, ge=0)
    human_minutes: Decimal = Field(default=Decimal(0), ge=Decimal(0))
    retained_subscriptions: int = Field(default=0, ge=0)
    metrics_version: str = METRICS_VERSION

    @property
    def net_recovered(self) -> Money:
        return net_recovered(self.gross_recovered, self.cost.total)

    @property
    def contacted_case_count(self) -> int:
        """Cases that received at least one contact. Bounded above by the number of
        cases and below by nothing we can derive, so it is approximated by the
        at-risk count when contacts occurred -- Phase 1 supplies the true figure."""
        return min(self.at_risk_case_count, self.outbound_contacts) or 0

    @model_validator(mode="after")
    def _counts_are_internally_possible(self) -> "ArmOutcome":
        if self.recovered_case_count > self.at_risk_case_count:
            raise ValueError(
                f"recovered_case_count {self.recovered_case_count} exceeds "
                f"at_risk_case_count {self.at_risk_case_count}"
            )
        if self.promises_kept > self.promises_made:
            raise ValueError("promises_kept exceeds promises_made")
        if self.gross_recovered > self.total_at_risk:
            raise ValueError(
                "gross_recovered exceeds total_at_risk; invariant #6 says we never "
                "collect more than is owed"
            )
        if self.false_actions > self.outbound_contacts:
            raise ValueError("false_actions exceeds outbound_contacts")
        # opt_out_rate and complaint_rate divide by contacted_case_count and *raise*
        # when the numerator is larger. Bounding them here makes an impossible
        # count a construction error; without it the ValueError surfaces from
        # inside guardrail_contract_holds, so the §13 guardrail crashes instead of
        # failing closed -- and a crashed guardrail in CI looks like a broken test
        # rather than a breached one.
        if self.opt_outs > self.contacted_case_count:
            raise ValueError(
                f"opt_outs {self.opt_outs} exceeds contacted_case_count "
                f"{self.contacted_case_count}"
            )
        if self.complaints > self.contacted_case_count:
            raise ValueError(
                f"complaints {self.complaints} exceeds contacted_case_count "
                f"{self.contacted_case_count}"
            )
        return self


# ---------------------------------------------------------------------------
# §13 formulas
# ---------------------------------------------------------------------------


def total_amount_at_risk(items: Iterable[tuple[str, Money]]) -> Money:
    """Sum at-risk over ``(obligation_id, amount)`` pairs, counting each obligation
    once (§13's anti-double-counting rule).

    A repeated obligation at a *different* amount is a data error and raises: one
    of the two numbers is wrong, and picking either silently would move the
    denominator of the headline metric.
    """
    seen: dict[str, Money] = {}
    for obligation_id, amount in items:
        previous = seen.get(obligation_id)
        if previous is None:
            seen[obligation_id] = amount
        elif previous != amount:
            raise ValueError(
                f"obligation {obligation_id!r} appears at two different at-risk "
                f"amounts ({previous} and {amount}); recognition happens once, at "
                "detection, so one of these is wrong"
            )
    return money_sum(seen.values())


def net_recovered(gross_recovered: Money, cost_to_collect: Money) -> Money:
    """§13: gross recovered - cost to collect. Deliberately not clamped at zero."""
    return gross_recovered - cost_to_collect


def _rate(numerator: Decimal | int, denominator: Decimal | int) -> Decimal | None:
    """A quantised rate, or None when there is no denominator (JC-33)."""
    if denominator == 0:
        return None
    return ratio(Decimal(numerator) / Decimal(denominator))


def recovery_rate(*, recovered_obligations: int, at_risk_obligations: int) -> Decimal | None:
    """§13: recovered obligations / at-risk obligations."""
    if recovered_obligations > at_risk_obligations:
        raise ValueError("recovered_obligations exceeds at_risk_obligations")
    return _rate(recovered_obligations, at_risk_obligations)


def false_action_rate(*, false_actions: int, outbound_contacts: int) -> Decimal | None:
    if false_actions > outbound_contacts:
        raise ValueError("false_actions exceeds outbound_contacts")
    return _rate(false_actions, outbound_contacts)


def contacts_per_recovery(*, outbound_contacts: int, recovered_cases: int) -> Decimal | None:
    return _rate(outbound_contacts, recovered_cases)


def cost_per_rupee_recovered(cost_to_collect: Money, gross_recovered: Money) -> Decimal | None:
    if gross_recovered.is_zero:
        return None
    return ratio(cost_to_collect.ratio_to(gross_recovered))


def human_minutes_per_100k_recovered(
    *, human_minutes: Decimal, gross_recovered: Money
) -> Decimal | None:
    if gross_recovered.is_zero:
        return None
    lakhs = gross_recovered.rupees / Decimal(RUPEES_PER_LAKH)
    return ratio(human_minutes / lakhs)


def promise_kept_rate(*, promises_kept: int, promises_made: int) -> Decimal | None:
    if promises_kept > promises_made:
        raise ValueError("promises_kept exceeds promises_made")
    return _rate(promises_kept, promises_made)


def escalation_rate(*, escalated_cases: int, total_cases: int) -> Decimal | None:
    if escalated_cases > total_cases:
        raise ValueError("escalated_cases exceeds total_cases")
    return _rate(escalated_cases, total_cases)


def opt_out_rate(*, opt_outs: int, contacted_cases: int) -> Decimal | None:
    if opt_outs > contacted_cases:
        raise ValueError("opt_outs exceeds contacted_cases")
    return _rate(opt_outs, contacted_cases)


def complaint_rate(*, complaints: int, contacted_cases: int) -> Decimal | None:
    if complaints > contacted_cases:
        raise ValueError("complaints exceeds contacted_cases")
    return _rate(complaints, contacted_cases)


def net_recovered_per_rupee_at_risk(outcome: ArmOutcome) -> Decimal | None:
    """The per-arm quantity the headline metric differences.

    ``None`` when the arm held no money at risk: an arm with an empty stratum has
    no rate, and must not contribute one (JC-33).
    """
    if outcome.total_at_risk.is_zero:
        return None
    return ratio(outcome.net_recovered.ratio_to(outcome.total_at_risk))


# ---------------------------------------------------------------------------
# The headline
# ---------------------------------------------------------------------------


class IncrementalRecoveryEstimate(BaseModel):
    """§13's headline, with the shape of its uncertainty fixed in Phase 0.

    ``ci_low``/``ci_high`` are optional *here* and mandatory on the scoreboard: the
    bootstrap needs case-level data (Phase 1). Freezing the fields now means a
    point estimate cannot be published as the headline with the interval quietly
    omitted (JC-36).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    treatment_arm: Arm
    control_arm: Arm
    per_rupee_delta: Ratio = Field(
        description="(net/at-risk) in treatment minus the same in control."
    )
    point: Money = Field(description="per_rupee_delta x total at risk.")
    total_at_risk: Money
    strata_count: int = Field(default=1, ge=1)
    ci_low: Money | None = None
    ci_high: Money | None = None
    resamples: int | None = Field(
        default=None,
        description=f"Set to {BOOTSTRAP_RESAMPLES} by the Phase 1 bootstrap.",
    )
    p_value: PValue | None = None
    metrics_version: str = METRICS_VERSION

    @model_validator(mode="after")
    def _interval_is_complete_and_ordered(self) -> "IncrementalRecoveryEstimate":
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("a confidence interval needs both bounds")
        if self.ci_low is not None and self.ci_high is not None:
            if self.ci_low > self.ci_high:
                raise ValueError("ci_low exceeds ci_high")
            if self.resamples is None:
                raise ValueError(
                    "an interval must say how many resamples produced it (§12.1: "
                    f"{BOOTSTRAP_RESAMPLES})"
                )
        return self

    @property
    def is_publishable_as_headline(self) -> bool:
        """§12.5: the headline is a number *with* an interval. The scoreboard
        asserts this before printing."""
        return self.ci_low is not None and self.resamples == BOOTSTRAP_RESAMPLES


def _delta(treatment: ArmOutcome, control: ArmOutcome) -> Decimal:
    if treatment.arm is control.arm:
        raise ValueError(
            f"treatment and control are both {treatment.arm.value}; an arm compared "
            "with itself is not a result"
        )
    treatment_rate = net_recovered_per_rupee_at_risk(treatment)
    control_rate = net_recovered_per_rupee_at_risk(control)
    if treatment_rate is None or control_rate is None:
        raise ValueError(
            "an arm with zero at-risk has no recovery rate; it cannot be "
            "differenced (JC-33)"
        )
    return treatment_rate - control_rate


def net_incremental_recovery(
    *, treatment: ArmOutcome, control: ArmOutcome, total_at_risk: Money
) -> IncrementalRecoveryEstimate:
    """§13's headline for a single stratum (or a pooled run).

    ``(net recovered / at risk)_t - (net recovered / at risk)_c, x total at risk``.
    """
    delta = _delta(treatment, control)
    return IncrementalRecoveryEstimate(
        treatment_arm=treatment.arm,
        control_arm=control.arm,
        per_rupee_delta=delta,
        point=total_at_risk * ratio(delta),
        total_at_risk=total_at_risk,
    )


def stratum_weighted_incremental_recovery(
    per_stratum: Mapping[StratumKey, tuple[ArmOutcome, ArmOutcome | None]],
    *,
    stratum_at_risk: Mapping[StratumKey, Money],
) -> IncrementalRecoveryEstimate:
    """§12.1's stratum-weighted headline.

    Each stratum contributes its own per-rupee delta, weighted by the at-risk money
    in that stratum (JC-35). An unmatched stratum -- present in one arm only -- is
    refused rather than treated as zero: it has no counterfactual, and assuming one
    fabricates lift.
    """
    if not per_stratum:
        raise ValueError("no strata supplied")

    total = money_sum(stratum_at_risk.values())
    if total.is_zero:
        raise ValueError("total at-risk across strata is zero")

    treatment_arms: set[Arm] = set()
    control_arms: set[Arm] = set()
    weighted = Decimal(0)

    for stratum, (treatment, control) in per_stratum.items():
        if control is None or treatment is None:
            raise ValueError(
                f"stratum {stratum.key} is present in one arm only; it has no "
                "counterfactual and cannot contribute to a difference"
            )
        if treatment.stratum != stratum or control.stratum != stratum:
            raise ValueError(
                f"an outcome filed under stratum {stratum.key} carries a different "
                "stratum of its own"
            )
        weight_money = stratum_at_risk.get(stratum)
        if weight_money is None:
            raise ValueError(
                f"stratum {stratum.key} has no declared at-risk total; the weights "
                "must be stated, not inferred from the arms being compared"
            )
        treatment_arms.add(treatment.arm)
        control_arms.add(control.arm)
        weighted += _delta(treatment, control) * weight_money.ratio_to(total)

    if len(treatment_arms) != 1 or len(control_arms) != 1:
        raise ValueError(
            f"strata compare different arm pairs: treatment={treatment_arms}, "
            f"control={control_arms}"
        )

    delta = ratio(weighted)
    return IncrementalRecoveryEstimate(
        treatment_arm=treatment_arms.pop(),
        control_arm=control_arms.pop(),
        per_rupee_delta=delta,
        point=total * delta,
        total_at_risk=total,
        strata_count=len(per_stratum),
    )


# ---------------------------------------------------------------------------
# The guardrail contract
# ---------------------------------------------------------------------------


class GuardrailCheck(BaseModel):
    """§13's guardrail contract, stated as a pass/fail with its reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    reason: str = Field(max_length=400)
    treatment_opt_out_rate: Ratio | None = None
    control_opt_out_rate: Ratio | None = None
    policy_violations: int = Field(default=0, ge=0)


def guardrail_contract_holds(*, treatment: ArmOutcome, control: ArmOutcome) -> GuardrailCheck:
    """§13, verbatim: "A policy change that improves net incremental recovery but
    increases the opt-out rate above the control arm's is rejected."

    Plus §13's hard zero on policy violations. Note the asymmetry: recovery is not
    an input here. A treatment that recovers more does not buy tolerance for a
    higher opt-out rate; that is the entire point of the contract.
    """
    if treatment.policy_violations > 0:
        return GuardrailCheck(
            passed=False,
            reason=(
                f"{treatment.policy_violations} policy violation(s) in the treatment "
                "arm; §13 requires exactly 0"
            ),
            policy_violations=treatment.policy_violations,
        )

    treatment_rate = opt_out_rate(
        opt_outs=treatment.opt_outs, contacted_cases=treatment.contacted_case_count
    )
    control_rate = opt_out_rate(
        opt_outs=control.opt_outs, contacted_cases=control.contacted_case_count
    )

    if treatment_rate is None or control_rate is None:
        return GuardrailCheck(
            passed=False,
            reason="an arm contacted nobody, so the opt-out rates cannot be "
            "compared; the guardrail fails closed",
            treatment_opt_out_rate=treatment_rate,
            control_opt_out_rate=control_rate,
        )

    if treatment_rate > control_rate:
        return GuardrailCheck(
            passed=False,
            reason=(
                f"treatment opt-out rate {treatment_rate} exceeds control "
                f"{control_rate}; recovery may not be bought with customer harm"
            ),
            treatment_opt_out_rate=treatment_rate,
            control_opt_out_rate=control_rate,
        )

    return GuardrailCheck(
        passed=True,
        reason=f"opt-out rate {treatment_rate} is at or below control {control_rate}",
        treatment_opt_out_rate=treatment_rate,
        control_opt_out_rate=control_rate,
    )
