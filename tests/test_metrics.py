"""Contract tests for the metric definitions module (deliverable #6).

These pin the §13 table, not an implementation. The point of the module is that a
finance judge can read one file and see that "net incremental recovery" means
exactly what the plan says it means, and that nothing in the codebase computes it a
second, slightly different way.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from reclaim.contracts.enums import Arm, RiskClass, Segment
from reclaim.contracts.metrics import (
    ArmOutcome,
    CostBreakdown,
    MetricDirection,
    MetricKey,
    MetricUnit,
    METRIC_SPECS,
    PLAN_SECTION_13_ROWS,
    complaint_rate,
    contacts_per_recovery,
    cost_per_rupee_recovered,
    escalation_rate,
    false_action_rate,
    guardrail_contract_holds,
    human_minutes_per_100k_recovered,
    metric_spec,
    net_incremental_recovery,
    net_recovered,
    net_recovered_per_rupee_at_risk,
    opt_out_rate,
    promise_kept_rate,
    recovery_rate,
    stratum_weighted_incremental_recovery,
    total_amount_at_risk,
)
from reclaim.contracts.money import Money
from reclaim.contracts.strata import StratumKey


def _stratum(band_amount: int = 1499, segment: Segment = Segment.B2C_STANDARD) -> StratumKey:
    return StratumKey.build(
        amount=Money.from_rupees(band_amount),
        failure_class=RiskClass.FAILED_RECURRING_DEBIT,
        segment=segment,
    )


def _cost(**kw) -> CostBreakdown:
    kwargs = dict(
        psp_fees=Money.from_rupees(20),
        failed_attempt_fees=Money.from_rupees(10),
        channel_cost=Money.from_rupees(5),
        llm_and_infra_cost=Money.from_rupees(3),
        human_minutes=Decimal("0"),
        human_loaded_rate_per_hour=Money.from_rupees(600),
    )
    kwargs.update(kw)
    return CostBreakdown(**kwargs)


def _outcome(**kw) -> ArmOutcome:
    kwargs = dict(
        arm=Arm.A4,
        stratum=_stratum(),
        at_risk_case_count=100,
        recovered_case_count=30,
        total_at_risk=Money.from_rupees(100000),
        gross_recovered=Money.from_rupees(30000),
        cost=_cost(),
        outbound_contacts=180,
        escalations=6,
        promises_made=20,
        promises_kept=15,
        opt_outs=2,
        complaints=1,
        policy_violations=0,
        false_actions=3,
        human_minutes=Decimal("45"),
    )
    kwargs.update(kw)
    return ArmOutcome(**kwargs)


# ------------------------------------------------------- the §13 table itself


def test_every_row_of_plan_section_13_is_covered_by_a_metric():
    """'Matching §13 exactly' is checkable, so it is checked: every row label in
    the plan's table maps to at least one MetricKey, and no metric invents a row."""
    covered = {spec.plan_row for spec in METRIC_SPECS.values()}
    assert covered == set(PLAN_SECTION_13_ROWS)


def test_the_plan_has_sixteen_rows_and_we_define_seventeen_metrics():
    """The one place the table is not 1:1 with code: 'Opt-out & complaint rate' is
    a single row naming two separately measured quantities."""
    assert len(PLAN_SECTION_13_ROWS) == 16
    assert len(MetricKey) == 17
    split = [k for k, s in METRIC_SPECS.items() if s.plan_row == "Opt-out & complaint rate"]
    assert set(split) == {MetricKey.OPT_OUT_RATE, MetricKey.COMPLAINT_RATE}


def test_every_metric_has_a_spec_with_a_formula_a_unit_and_a_direction():
    assert set(METRIC_SPECS) == set(MetricKey)
    for key, spec in METRIC_SPECS.items():
        assert spec.formula.strip(), f"{key} has no formula"
        assert isinstance(spec.unit, MetricUnit)
        assert isinstance(spec.direction, MetricDirection)


def test_exactly_one_metric_is_the_headline():
    headline = [k for k, s in METRIC_SPECS.items() if s.is_headline]
    assert headline == [MetricKey.NET_INCREMENTAL_RECOVERY]


def test_policy_violations_is_a_count_that_must_be_zero():
    """§13: 'must be 0. Reported as a hard number, not a rate.'"""
    spec = metric_spec(MetricKey.POLICY_VIOLATIONS)
    assert spec.unit is MetricUnit.COUNT
    assert spec.direction is MetricDirection.MUST_BE_ZERO


def test_guardrail_metrics_are_marked_as_such():
    """§13 requires them reported *beside* recovery, not folded into it."""
    for key in (
        MetricKey.OPT_OUT_RATE,
        MetricKey.COMPLAINT_RATE,
        MetricKey.FALSE_ACTION_RATE,
        MetricKey.CONTACTS_PER_RECOVERY,
        MetricKey.POLICY_VIOLATIONS,
    ):
        assert metric_spec(key).is_guardrail is True


def test_retained_subscriptions_carries_its_caveat():
    """§13: 'secondary, with an explicit survival-analysis caveat. We do not claim
    ARR saved.' The caveat travels with the metric or it will be dropped."""
    spec = metric_spec(MetricKey.RETAINED_SUBSCRIPTIONS)
    assert spec.is_headline is False
    assert "ARR" in spec.caveat


def test_money_metrics_are_denominated_in_rupees_not_ratios():
    for key in (
        MetricKey.AMOUNT_AT_RISK,
        MetricKey.GROSS_RECOVERED,
        MetricKey.COST_TO_COLLECT,
        MetricKey.NET_RECOVERED,
        MetricKey.NET_INCREMENTAL_RECOVERY,
    ):
        assert metric_spec(key).unit is MetricUnit.MONEY


# ------------------------------------------------------------ anti-double-count


def test_amount_at_risk_is_recognised_once_per_obligation():
    """§13's anti-double-counting rule. The same obligation detected twice (a
    re-detection after a retry) contributes once."""
    total = total_amount_at_risk(
        [
            ("obl_1", Money.from_rupees(1000)),
            ("obl_2", Money.from_rupees(2000)),
            ("obl_1", Money.from_rupees(1000)),
        ]
    )
    assert total == Money.from_rupees(3000)


def test_the_same_obligation_at_two_different_amounts_is_a_data_error():
    """Silently picking one would understate or overstate the denominator of the
    headline metric. It has to be loud."""
    with pytest.raises(ValueError):
        total_amount_at_risk(
            [("obl_1", Money.from_rupees(1000)), ("obl_1", Money.from_rupees(1500))]
        )


def test_an_incident_at_risk_is_not_additive_on_top_of_its_members():
    """§13: 'A systemic incident's at-risk equals the sum of its member cases — it
    is not additive on top.' Passing member cases plus an incident total must not
    double it, so the incident is expressed as its members."""
    members = [("obl_1", Money.from_rupees(1000)), ("obl_2", Money.from_rupees(2000))]
    assert total_amount_at_risk(members) == Money.from_rupees(3000)
    assert total_amount_at_risk(members + members) == Money.from_rupees(3000)


# ------------------------------------------------------------------- formulas


def test_net_recovered_is_gross_minus_cost():
    assert net_recovered(Money.from_rupees(1000), Money.from_rupees(120)) == Money.from_rupees(880)


def test_net_recovered_may_be_negative():
    """Spending more than we recover is a real outcome and must be reportable, not
    clamped to zero. Clamping is how a losing arm looks break-even."""
    assert net_recovered(Money.from_rupees(100), Money.from_rupees(400)).paise < 0


def test_cost_to_collect_sums_the_five_components_in_plan_section_13():
    cost = _cost(human_minutes=Decimal("30"))
    expected = (
        Money.from_rupees(20)
        + Money.from_rupees(10)
        + Money.from_rupees(5)
        + Money.from_rupees(3)
        + Money.from_rupees(300)  # 30 min at Rs 600/hr
    )
    assert cost.total == expected


def test_human_cost_is_exact_at_the_paise_boundary():
    """A loaded rate that does not divide evenly into minutes must not drift."""
    cost = _cost(human_minutes=Decimal("7"), human_loaded_rate_per_hour=Money.from_rupees(1000))
    assert cost.human_cost == Money.from_paise(11667)


def test_recovery_rate_is_obligations_not_rupees():
    """§13 defines it as 'recovered obligations / at-risk obligations'. Using
    rupees here would silently make it a value-weighted rate."""
    assert recovery_rate(recovered_obligations=30, at_risk_obligations=100) == Decimal("0.300000")


def test_a_rate_with_a_zero_denominator_is_none_not_zero():
    """Zero is a claim; None is the absence of one. An empty stratum reporting a
    0% recovery rate would drag a weighted average down with no data behind it."""
    assert recovery_rate(recovered_obligations=0, at_risk_obligations=0) is None
    assert contacts_per_recovery(outbound_contacts=10, recovered_cases=0) is None
    assert cost_per_rupee_recovered(Money.from_rupees(50), Money.zero()) is None
    assert false_action_rate(false_actions=0, outbound_contacts=0) is None


def test_rates_are_fixed_scale_decimals_not_floats():
    """They reach the audit chain and the scoreboard; canonical_json rejects
    floats (JC-15)."""
    rate = recovery_rate(recovered_obligations=1, at_risk_obligations=3)
    assert isinstance(rate, Decimal)
    assert str(rate) == "0.333333"


def test_contacts_per_recovery_and_cost_per_rupee_may_exceed_one():
    assert contacts_per_recovery(outbound_contacts=180, recovered_cases=30) == Decimal("6.000000")
    assert cost_per_rupee_recovered(Money.from_rupees(200), Money.from_rupees(100)) == Decimal("2.000000")


def test_human_minutes_per_100k_recovered_scales_by_a_lakh():
    value = human_minutes_per_100k_recovered(
        human_minutes=Decimal("50"), gross_recovered=Money.from_rupees(200000)
    )
    assert value == Decimal("25.000000")


def test_promise_escalation_optout_and_complaint_rates():
    assert promise_kept_rate(promises_kept=15, promises_made=20) == Decimal("0.750000")
    assert escalation_rate(escalated_cases=6, total_cases=100) == Decimal("0.060000")
    assert opt_out_rate(opt_outs=2, contacted_cases=100) == Decimal("0.020000")
    assert complaint_rate(complaints=1, contacted_cases=100) == Decimal("0.010000")


def test_promises_kept_cannot_exceed_promises_made():
    with pytest.raises(ValueError):
        promise_kept_rate(promises_kept=21, promises_made=20)


# --------------------------------------------------------- the headline metric


def test_net_incremental_recovery_matches_the_plan_formula():
    """(net/at risk)_t − (net/at risk)_c × total at risk."""
    treatment = _outcome(
        arm=Arm.A4, total_at_risk=Money.from_rupees(100000), gross_recovered=Money.from_rupees(30000)
    )
    control = _outcome(
        arm=Arm.A1, total_at_risk=Money.from_rupees(100000), gross_recovered=Money.from_rupees(20000)
    )
    estimate = net_incremental_recovery(
        treatment=treatment, control=control, total_at_risk=Money.from_rupees(100000)
    )
    # Both arms carry the same Rs 38 of cost, so the per-rupee delta is the gross
    # delta: 10,000 / 100,000 = 0.1 -> Rs 10,000 on a Rs 100,000 book.
    assert estimate.per_rupee_delta == Decimal("0.100000")
    assert estimate.point == Money.from_rupees(10000)
    assert estimate.control_arm is Arm.A1
    assert estimate.treatment_arm is Arm.A4


def test_a_treatment_that_spends_more_than_it_recovers_shows_a_negative_increment():
    treatment = _outcome(
        gross_recovered=Money.from_rupees(21000), cost=_cost(channel_cost=Money.from_rupees(5000))
    )
    control = _outcome(arm=Arm.A1, gross_recovered=Money.from_rupees(20000))
    estimate = net_incremental_recovery(
        treatment=treatment, control=control, total_at_risk=Money.from_rupees(100000)
    )
    assert estimate.point.paise < 0


def test_the_increment_requires_two_different_arms():
    """Comparing an arm with itself is always zero and is never a result."""
    outcome = _outcome()
    with pytest.raises(ValueError):
        net_incremental_recovery(
            treatment=outcome, control=outcome, total_at_risk=Money.from_rupees(100000)
        )


def test_net_recovered_per_rupee_at_risk_of_an_empty_arm_is_none():
    empty = _outcome(
        at_risk_case_count=0,
        recovered_case_count=0,
        total_at_risk=Money.zero(),
        gross_recovered=Money.zero(),
        outbound_contacts=0,
        escalations=0,
        promises_made=0,
        promises_kept=0,
        opt_outs=0,
        complaints=0,
        false_actions=0,
    )
    assert net_recovered_per_rupee_at_risk(empty) is None


def test_an_arm_that_sent_nothing_cannot_have_sent_a_false_action():
    """A false action is a *contact* sent wrongly (§13's definition), so it is
    bounded by outbound contacts. Without this, an empty stratum can report a
    false-action count with no denominator to divide it by."""
    with pytest.raises(ValidationError):
        _outcome(outbound_contacts=0, false_actions=1)


# --------------------------------------------------- stratum-weighted estimate


def test_stratum_weighting_equals_the_pooled_result_when_strata_are_identical():
    """A sanity property: weighting cannot invent a difference that is not there."""
    a, b = _stratum(1499), _stratum(50000)
    per_stratum = {
        a: (
            _outcome(stratum=a, gross_recovered=Money.from_rupees(15000), total_at_risk=Money.from_rupees(50000)),
            _outcome(arm=Arm.A1, stratum=a, gross_recovered=Money.from_rupees(10000), total_at_risk=Money.from_rupees(50000)),
        ),
        b: (
            _outcome(stratum=b, gross_recovered=Money.from_rupees(15000), total_at_risk=Money.from_rupees(50000)),
            _outcome(arm=Arm.A1, stratum=b, gross_recovered=Money.from_rupees(10000), total_at_risk=Money.from_rupees(50000)),
        ),
    }
    estimate = stratum_weighted_incremental_recovery(
        per_stratum, stratum_at_risk={a: Money.from_rupees(50000), b: Money.from_rupees(50000)}
    )
    assert estimate.per_rupee_delta == Decimal("0.100000")
    assert estimate.point == Money.from_rupees(10000)
    assert estimate.strata_count == 2


def test_stratum_weighting_follows_the_at_risk_share_not_the_case_count():
    """A stratum holding 90% of the money must dominate, even if it holds few
    cases. Case-count weighting is how a high-value regression hides."""
    small, large = _stratum(1499), _stratum(500000)
    per_stratum = {
        small: (
            _outcome(stratum=small, gross_recovered=Money.from_rupees(2000), total_at_risk=Money.from_rupees(10000)),
            _outcome(arm=Arm.A1, stratum=small, gross_recovered=Money.zero(), total_at_risk=Money.from_rupees(10000)),
        ),
        large: (
            _outcome(stratum=large, gross_recovered=Money.zero(), total_at_risk=Money.from_rupees(90000)),
            _outcome(arm=Arm.A1, stratum=large, gross_recovered=Money.zero(), total_at_risk=Money.from_rupees(90000)),
        ),
    }
    estimate = stratum_weighted_incremental_recovery(
        per_stratum,
        stratum_at_risk={small: Money.from_rupees(10000), large: Money.from_rupees(90000)},
    )
    # +Rs 2,000 on a Rs 10,000 stratum, nothing on the Rs 90,000 stratum.
    assert estimate.point == Money.from_rupees(2000)


def test_a_stratum_present_in_one_arm_only_is_refused():
    """An unmatched stratum has no counterfactual. Silently treating the missing
    arm as zero would fabricate a lift."""
    a = _stratum()
    with pytest.raises(ValueError):
        stratum_weighted_incremental_recovery(
            {a: (_outcome(stratum=a), None)}, stratum_at_risk={a: Money.from_rupees(1000)}
        )


def test_a_stratum_with_no_declared_target_at_risk_is_refused():
    a = _stratum()
    with pytest.raises(ValueError):
        stratum_weighted_incremental_recovery(
            {a: (_outcome(stratum=a), _outcome(arm=Arm.A1, stratum=a))}, stratum_at_risk={}
        )


def test_an_outcome_filed_under_the_wrong_stratum_is_refused():
    a, b = _stratum(1499), _stratum(500000)
    with pytest.raises(ValueError):
        stratum_weighted_incremental_recovery(
            {a: (_outcome(stratum=b), _outcome(arm=Arm.A1, stratum=a))},
            stratum_at_risk={a: Money.from_rupees(1000)},
        )


# ------------------------------------------------------- the guardrail contract


def test_the_guardrail_contract_rejects_recovery_bought_with_opt_outs():
    """§13, verbatim: 'A policy change that improves net incremental recovery but
    increases the opt-out rate above the control arm's is rejected.'"""
    treatment = _outcome(opt_outs=9, gross_recovered=Money.from_rupees(40000))
    control = _outcome(arm=Arm.A1, opt_outs=2, gross_recovered=Money.from_rupees(20000))
    check = guardrail_contract_holds(treatment=treatment, control=control)
    assert check.passed is False
    assert "opt-out" in check.reason.lower()


def test_the_guardrail_contract_passes_when_opt_outs_do_not_rise():
    treatment = _outcome(opt_outs=1, gross_recovered=Money.from_rupees(40000))
    control = _outcome(arm=Arm.A1, opt_outs=2, gross_recovered=Money.from_rupees(20000))
    assert guardrail_contract_holds(treatment=treatment, control=control).passed is True


def test_the_guardrail_contract_fails_on_any_policy_violation():
    """§13: policy violations must be 0. One is a failed run, not a worse score."""
    treatment = _outcome(policy_violations=1)
    control = _outcome(arm=Arm.A1)
    check = guardrail_contract_holds(treatment=treatment, control=control)
    assert check.passed is False


def test_an_equal_opt_out_rate_passes():
    """'Above the control arm's' is strict: equal is not worse."""
    treatment = _outcome(opt_outs=2)
    control = _outcome(arm=Arm.A1, opt_outs=2)
    assert guardrail_contract_holds(treatment=treatment, control=control).passed is True


# ------------------------------------------------------------- serialisability


def test_every_reported_object_is_canonically_serialisable():
    from reclaim.contracts.canonical import canonical_json

    canonical_json(_outcome())
    canonical_json(
        net_incremental_recovery(
            treatment=_outcome(),
            control=_outcome(arm=Arm.A1, gross_recovered=Money.from_rupees(20000)),
            total_at_risk=Money.from_rupees(100000),
        )
    )
    canonical_json([spec for spec in METRIC_SPECS.values()])


def test_an_outcome_rejects_impossible_counts():
    with pytest.raises(ValidationError):
        _outcome(recovered_case_count=101, at_risk_case_count=100)
    with pytest.raises(ValidationError):
        _outcome(promises_kept=21, promises_made=20)
    with pytest.raises(ValidationError):
        _outcome(gross_recovered=Money.from_rupees(200000), total_at_risk=Money.from_rupees(100000))


def test_opt_outs_cannot_exceed_the_cases_we_contacted():
    """§13's opt-out rate divides by contacted cases, and ``opt_out_rate`` raises
    when the numerator is larger. Without a bound at construction that ValueError
    escapes from inside ``guardrail_contract_holds``: the §13 guardrail *crashes*
    instead of failing closed, and a crashed guardrail in CI is indistinguishable
    from a broken test rather than a breached one."""
    with pytest.raises(ValidationError):
        _outcome(outbound_contacts=1, opt_outs=2, complaints=0, false_actions=0)


def test_complaints_cannot_exceed_the_cases_we_contacted():
    """Same bound, same reason: ``complaint_rate`` shares the denominator."""
    with pytest.raises(ValidationError):
        _outcome(outbound_contacts=1, complaints=2, opt_outs=0, false_actions=0)
