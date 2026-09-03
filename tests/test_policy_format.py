"""Contract tests for the policy rule *format* (deliverable #4).

Structure only. There are deliberately no real rules here: the rule set is Phase 1
work. What these tests pin is that the format can express §14.1's eight categories,
that a rule cannot be written without its own allow/deny test cases, that conflicts
fail closed, and that the whole thing is evaluable over facts the LLM cannot forge.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from reclaim.contracts.enums import (
    AutonomyTier,
    PolicyCategory,
    PolicyEffect,
    RuleSeverity,
    Segment,
)
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import ConsentProfile, QuietHours
from reclaim.contracts.policy_format import (
    DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT,
    ENUM_FACT_VOCABULARIES,
    FALLBACK_QUIET_HOURS_TIMEZONE,
    ComparisonOperator,
    FactPredicate,
    FactType,
    PolicyDecision,
    PolicyFactKey,
    PolicyRule,
    PolicyRuleSet,
    PolicyThresholds,
    PolicyVerdict,
    Predicate,
    RuleTestCase,
    combine_verdicts,
    fact_type,
    resolve_quiet_hours,
)

_T0 = datetime(2026, 3, 1, 4, 30, tzinfo=timezone.utc)


def _predicate(key: PolicyFactKey = PolicyFactKey.HAS_CHANNEL_CONSENT, value=False) -> Predicate:
    return FactPredicate(fact=key, operator=ComparisonOperator.EQ, value=value)


def _rule(**overrides) -> PolicyRule:
    kwargs = dict(
        rule_id="POL-CONSENT-001",
        category=PolicyCategory.CONSENT_AND_CHANNEL,
        title="No contact without channel consent",
        effect=PolicyEffect.DENY,
        severity=RuleSeverity.BLOCKING,
        when=_predicate(),
        human_reason="We do not have this customer's consent for this channel.",
        citation="HACKATHON_PLAN.md §14.1 consent & channel",
        test_cases=[
            RuleTestCase(
                name="absent consent denies",
                facts={PolicyFactKey.HAS_CHANNEL_CONSENT: False},
                expect_matches=True,
            ),
            RuleTestCase(
                name="present consent does not match",
                facts={PolicyFactKey.HAS_CHANNEL_CONSENT: True},
                expect_matches=False,
            ),
        ],
    )
    kwargs.update(overrides)
    return PolicyRule(**kwargs)


# ------------------------------------------------------------ rule structure


def test_a_rule_carries_a_stable_id_a_category_and_a_human_reason():
    """§14.1 verdicts are `DENY(rule_id, human_reason)`. Both are required: an
    unexplained denial is unusable in an approval queue."""
    rule = _rule()
    assert rule.rule_id == "POL-CONSENT-001"
    assert rule.category is PolicyCategory.CONSENT_AND_CHANNEL
    assert rule.human_reason


def test_a_denying_rule_without_a_human_reason_is_invalid():
    with pytest.raises(ValidationError):
        _rule(human_reason="")


def test_rule_ids_are_shape_constrained():
    with pytest.raises(ValidationError):
        _rule(rule_id="whatever")


def test_every_rule_must_ship_with_an_allow_case_and_a_deny_case():
    """§14.1: rules are 'individually tested'. A rule with only matching cases
    has never been shown to *not* fire, which is how over-blocking ships."""
    with pytest.raises(ValidationError):
        _rule(
            test_cases=[
                RuleTestCase(
                    name="only a positive case",
                    facts={PolicyFactKey.HAS_CHANNEL_CONSENT: False},
                    expect_matches=True,
                )
            ]
        )


def test_a_rule_may_not_reference_a_fact_outside_the_closed_vocabulary():
    """Facts are a closed set the LLM cannot influence. A typo'd fact name must
    be a load error, not a silently never-matching rule."""
    with pytest.raises(ValidationError):
        FactPredicate(fact="customer_seems_annoyed", operator=ComparisonOperator.EQ, value=True)


def test_a_rule_test_case_may_not_reference_an_unknown_fact():
    with pytest.raises(ValidationError):
        RuleTestCase(name="bad", facts={"invented_fact": True}, expect_matches=True)


def test_defer_rules_must_say_what_they_are_waiting_for():
    """DEFER(until) is meaningless without the `until`."""
    with pytest.raises(ValidationError):
        _rule(effect=PolicyEffect.DEFER, defer_until_fact=None)

    rule = _rule(
        effect=PolicyEffect.DEFER,
        defer_until_fact=PolicyFactKey.QUIET_HOURS_END_AT,
        human_reason="Outside quiet hours; will send when the window opens.",
    )
    assert rule.defer_until_fact is PolicyFactKey.QUIET_HOURS_END_AT


def test_approval_rules_declare_the_tier_they_require():
    rule = _rule(
        effect=PolicyEffect.ALLOW_WITH_APPROVAL,
        requires_tier=AutonomyTier.T2,
        human_reason="Above the AFA threshold; a human must approve the debit.",
    )
    assert rule.requires_tier is AutonomyTier.T2


def test_a_plain_allow_rule_may_not_demand_an_approval_tier():
    with pytest.raises(ValidationError):
        _rule(effect=PolicyEffect.ALLOW, requires_tier=AutonomyTier.T2)


# --------------------------------------------------------- predicate algebra


def test_predicates_compose_with_all_of_any_of_and_not():
    from reclaim.contracts.policy_format import AllOf, AnyOf, Not

    tree = AllOf(
        all_of=[
            _predicate(),
            AnyOf(
                any_of=[
                    _predicate(PolicyFactKey.IS_OPTED_OUT, True),
                    Not(negate=_predicate(PolicyFactKey.RECONCILIATION_IS_FRESH, True)),
                ]
            ),
        ]
    )
    assert len(tree.all_of) == 2


def test_an_empty_all_of_is_rejected():
    """An empty conjunction is vacuously true, which would silently match every
    case. Fail closed at load time instead."""
    from reclaim.contracts.policy_format import AllOf

    with pytest.raises(ValidationError):
        AllOf(all_of=[])


def test_predicate_tree_depth_is_bounded():
    """An unbounded tree is an unreviewable rule."""
    from reclaim.contracts.policy_format import AllOf

    node: object = _predicate()
    for _ in range(12):
        node = AllOf(all_of=[node])
    with pytest.raises(ValidationError):
        _rule(when=node)


def test_money_comparisons_use_money_not_numbers():
    """A threshold written as a bare number is an ambiguity between rupees and
    paise, and that ambiguity is a 100x error."""
    predicate = FactPredicate(
        fact=PolicyFactKey.OBLIGATION_OUTSTANDING,
        operator=ComparisonOperator.GT,
        value=Money.from_rupees(15000),
    )
    assert predicate.value == Money.from_rupees(15000)


def test_a_money_fact_cannot_be_compared_to_a_bare_integer():
    with pytest.raises(ValidationError):
        FactPredicate(
            fact=PolicyFactKey.OBLIGATION_OUTSTANDING,
            operator=ComparisonOperator.GT,
            value=15000,
        )


def test_a_boolean_fact_cannot_be_compared_with_an_ordering_operator():
    with pytest.raises(ValidationError):
        FactPredicate(
            fact=PolicyFactKey.IS_OPTED_OUT, operator=ComparisonOperator.GT, value=True
        )


# -------------------------------------------------------------- combination


def _verdict(effect: PolicyEffect, rule_id: str = "POL-X-001", **kw) -> PolicyVerdict:
    return PolicyVerdict(
        rule_id=rule_id,
        category=PolicyCategory.HOLDS,
        effect=effect,
        human_reason="because",
        **kw,
    )


def test_a_single_deny_beats_any_number_of_allows():
    decision = combine_verdicts(
        [
            _verdict(PolicyEffect.ALLOW, "POL-A-001"),
            _verdict(PolicyEffect.ALLOW, "POL-A-002"),
            _verdict(PolicyEffect.DENY, "POL-HOLD-001"),
        ]
    )
    assert decision.effect is PolicyEffect.DENY
    assert decision.deciding_rule_id == "POL-HOLD-001"


def test_deny_beats_defer_and_defer_beats_approval():
    assert combine_verdicts(
        [_verdict(PolicyEffect.DEFER), _verdict(PolicyEffect.DENY, "POL-D-001")]
    ).effect is PolicyEffect.DENY
    assert combine_verdicts(
        [_verdict(PolicyEffect.ALLOW_WITH_APPROVAL), _verdict(PolicyEffect.DEFER, "POL-T-001")]
    ).effect is PolicyEffect.DEFER


def test_the_latest_defer_time_wins():
    """Two deferrals mean waiting for both, not for the earlier one."""
    early = _verdict(PolicyEffect.DEFER, "POL-T-001", defer_until=_T0)
    late = _verdict(PolicyEffect.DEFER, "POL-T-002", defer_until=_T0 + timedelta(hours=5))
    decision = combine_verdicts([early, late])
    assert decision.defer_until == late.defer_until


def test_the_strictest_required_tier_wins():
    decision = combine_verdicts(
        [
            _verdict(PolicyEffect.ALLOW_WITH_APPROVAL, "POL-A-001", requires_tier=AutonomyTier.T1),
            _verdict(PolicyEffect.ALLOW_WITH_APPROVAL, "POL-A-002", requires_tier=AutonomyTier.T2),
        ]
    )
    assert decision.requires_tier is AutonomyTier.T2


def test_no_verdicts_at_all_fails_closed():
    """§14.1: rule conflicts fail closed. An action nothing evaluated is an
    action no rule permitted, not an action that is fine."""
    decision = combine_verdicts([])
    assert decision.effect is PolicyEffect.DENY
    assert decision.failed_closed is True


def test_an_advisory_verdict_never_blocks():
    decision = combine_verdicts(
        [
            _verdict(PolicyEffect.ALLOW, "POL-A-001"),
            PolicyVerdict(
                rule_id="POL-ADV-001",
                category=PolicyCategory.CONTENT,
                effect=PolicyEffect.DENY,
                human_reason="tone seems brusque",
                severity=RuleSeverity.ADVISORY,
            ),
        ]
    )
    assert decision.effect is PolicyEffect.ALLOW
    assert "POL-ADV-001" in decision.advisory_rule_ids


def test_every_verdict_is_retained_including_allows():
    """§14.1: 'Every verdict is logged, including allows.'"""
    verdicts = [_verdict(PolicyEffect.ALLOW, "POL-A-001"), _verdict(PolicyEffect.DENY, "POL-D-001")]
    decision = combine_verdicts(verdicts)
    assert [v.rule_id for v in decision.verdicts] == ["POL-A-001", "POL-D-001"]


def test_combination_is_order_independent():
    a = _verdict(PolicyEffect.DENY, "POL-D-001")
    b = _verdict(PolicyEffect.ALLOW, "POL-A-001")
    assert combine_verdicts([a, b]).effect is combine_verdicts([b, a]).effect


# ---------------------------------------------------------------- rule sets


def test_a_rule_set_rejects_duplicate_rule_ids():
    with pytest.raises(ValidationError):
        PolicyRuleSet(
            policy_version="1.0.0",
            rules=[_rule(), _rule(title="A different rule with the same id")],
        )


def test_a_rule_set_exposes_rules_by_category():
    rule_set = PolicyRuleSet(policy_version="1.0.0", rules=[_rule()])
    assert rule_set.by_category(PolicyCategory.CONSENT_AND_CHANNEL) == (rule_set.rules[0],)
    assert rule_set.by_category(PolicyCategory.TIMING) == ()


def test_a_rule_set_is_content_addressed():
    """Every decision records the policy version that produced it (§15). The
    digest catches an edited rule set that forgot to bump its version."""
    one = PolicyRuleSet(policy_version="1.0.0", rules=[_rule()])
    two = PolicyRuleSet(policy_version="1.0.0", rules=[_rule(title="Edited title")])
    assert one.digest != two.digest


def test_an_empty_rule_set_is_rejected():
    with pytest.raises(ValidationError):
        PolicyRuleSet(policy_version="1.0.0", rules=[])


# ---------------------------------------------------------------- thresholds


def test_thresholds_are_money_typed_and_have_plan_defaults():
    thresholds = PolicyThresholds()
    assert thresholds.t0_auto_reschedule_ceiling == Money.from_rupees(2000)
    assert thresholds.afa_required_above == Money.from_rupees(15000)
    assert thresholds.quiet_hours_start_local.hour == 9
    assert thresholds.quiet_hours_end_local.hour == 19


def test_thresholds_reject_a_negative_safety_margin():
    """Configuration may only ever make us more cautious than the rail floor."""
    with pytest.raises(ValidationError):
        PolicyThresholds(pre_debit_notification_safety_margin_hours=-1)


def test_thresholds_reject_an_inverted_quiet_hours_window():
    from datetime import time

    with pytest.raises(ValidationError):
        PolicyThresholds(quiet_hours_start_local=time(20, 0), quiet_hours_end_local=time(9, 0))


def test_concession_authority_is_structurally_zero():
    """Invariant #7. Not a default a YAML file can raise -- there is no field to
    raise, and the constant is asserted."""
    thresholds = PolicyThresholds()
    assert thresholds.max_concession_value == Money.zero()
    with pytest.raises(ValidationError):
        PolicyThresholds(max_concession_value=Money.from_rupees(1))


def test_decision_is_canonically_serialisable():
    from reclaim.contracts.canonical import canonical_json

    decision = combine_verdicts([_verdict(PolicyEffect.DENY, "POL-D-001")])
    assert isinstance(decision, PolicyDecision)
    canonical_json(decision)


def test_a_probability_fact_compares_against_a_quantised_decimal():
    """§14.2's "low confidence tiers up" rule is a predicate on
    ``diagnosis_confidence``, which is a PROBABILITY. With no Decimal in the
    predicate's value union, pydantic reaches for the next union member that
    accepts a number -- UtcDatetime -- and Decimal("0.55") becomes
    1970-01-01T00:00:00.55Z. The rule then silently compares a confidence score
    against an instant in 1970 and matches everything."""
    predicate = FactPredicate(
        fact=PolicyFactKey.DIAGNOSIS_CONFIDENCE,
        operator=ComparisonOperator.LT,
        value=Decimal("0.55"),
    )
    assert isinstance(predicate.value, Decimal)
    assert predicate.value == Decimal("0.550000")


def test_a_probability_fact_rejects_a_float_and_a_bare_string():
    """JC-15: no float enters a contract. And a bare string would make the
    comparison lexicographic, where "0.9" < "0.55" is False -- the rule would
    read as intended and evaluate backwards."""
    for bad in (0.55, "0.55"):
        with pytest.raises(ValidationError):
            FactPredicate(
                fact=PolicyFactKey.DIAGNOSIS_CONFIDENCE,
                operator=ComparisonOperator.LT,
                value=bad,
            )


def test_a_probability_outside_zero_to_one_is_rejected():
    """A confidence threshold of 1.5 can never fire; a rule that can never fire is
    worse than a missing rule because it reads as coverage."""
    with pytest.raises(ValidationError):
        FactPredicate(
            fact=PolicyFactKey.DIAGNOSIS_CONFIDENCE,
            operator=ComparisonOperator.LT,
            value=Decimal("1.5"),
        )


# ------------------------------------------- N1: the grace-period matrix is total


def test_the_default_grace_period_matrix_covers_every_segment():
    """Walks the enum rather than spot-checking a row.

    §14.2's authority matrix is looked up *by segment*, so a missing row is a
    KeyError raised inside the policy engine on the one case that carries the new
    segment -- not at load, and not on the segments anybody tested.
    """
    assert set(DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT) == set(Segment)
    assert set(PolicyThresholds().max_grace_period_days_by_segment) == set(Segment)
    for segment in Segment:
        assert DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT[segment] >= 0


def test_a_config_omitting_a_segment_is_refused_at_load():
    """The N1 defect. This mapping is a YAML-loaded *field* with a
    ``default_factory``, which is exactly why §6's sweep for non-total enum-keyed
    tables did not see it: an import-time guard can only ever check the default.
    """
    partial = {s: 7 for s in Segment if s is not Segment.B2B_STRATEGIC}
    with pytest.raises(ValidationError) as excinfo:
        PolicyThresholds(max_grace_period_days_by_segment=partial)
    assert "b2b_strategic" in str(excinfo.value)


def test_every_single_segment_omission_is_caught_one_at_a_time():
    """Each segment dropped on its own, so a guard that only notices an empty
    mapping -- or only the segment the author happened to try -- fails here."""
    for missing in Segment:
        partial = {s: 7 for s in Segment if s is not missing}
        with pytest.raises(ValidationError):
            PolicyThresholds(max_grace_period_days_by_segment=partial)


def test_a_complete_non_default_grace_period_config_is_still_accepted():
    """The guard against over-rejecting: totality is the requirement, not
    equality with the default. A config may be stricter than the one we ship."""
    stricter = {s: 1 for s in Segment}
    thresholds = PolicyThresholds(max_grace_period_days_by_segment=stricter)
    assert thresholds.max_grace_period_days_by_segment[Segment.B2B_STRATEGIC] == 1


# --------------------------------------------------- N2: timestamp predicates


def test_a_timestamp_fact_compares_against_an_aware_datetime():
    predicate = FactPredicate(
        fact=PolicyFactKey.QUIET_HOURS_END_AT,
        operator=ComparisonOperator.GTE,
        value=_T0,
    )
    assert isinstance(predicate.value, datetime)
    assert predicate.value == _T0


def test_a_timestamp_fact_rejects_a_bare_number_and_a_string():
    """N2, and §6-2's defect class one fact type over.

    ``quiet_hours_end_at gte 5`` used to load: with no TIMESTAMP branch an int
    satisfied the value union as an int, and the rule compared an instant against
    the scalar 5. A string is refused for the same reason a probability string
    is -- it compares lexicographically, which is right only for zero-padded
    RFC3339, and ``'2026-9-05' < '2026-10-01'`` is False.
    """
    for bad in (5, "2026-03-01T04:30:00.000000Z", "2026-9-05"):
        with pytest.raises(ValidationError):
            FactPredicate(
                fact=PolicyFactKey.QUIET_HOURS_END_AT,
                operator=ComparisonOperator.GTE,
                value=bad,
            )


def test_a_naive_datetime_cannot_reach_a_timestamp_predicate():
    """A naive datetime assumes the server's zone, and the server's zone is not
    the payer's -- which is the entire subject of a quiet-hours rule."""
    with pytest.raises(ValidationError):
        FactPredicate(
            fact=PolicyFactKey.QUIET_HOURS_END_AT,
            operator=ComparisonOperator.GTE,
            value=datetime(2026, 3, 1, 4, 30),
        )


# -------------------------------------------------------- N2: enum predicates


def test_every_enum_fact_declares_the_vocabulary_it_is_checked_against():
    """Walked in both directions. A fact typed ENUM with no vocabulary falls
    through to "any string"; a vocabulary for a fact that is not ENUM-typed is a
    row nothing reads, which is how a registry starts lying about its consumers.
    """
    enum_facts = {f for f in PolicyFactKey if fact_type(f) is FactType.ENUM}
    assert enum_facts, "no ENUM facts; the assertion below would be vacuous"
    assert set(ENUM_FACT_VOCABULARIES) == enum_facts


def test_every_member_of_every_enum_vocabulary_is_accepted():
    """Walks every row of every vocabulary. A check keyed on the wrong enum would
    refuse legitimate rules, and refusing to load is not automatically the safe
    direction when the rule being refused is the one that denies."""
    for fact, vocabulary in ENUM_FACT_VOCABULARIES.items():
        for member in vocabulary:
            predicate = FactPredicate(
                fact=fact, operator=ComparisonOperator.EQ, value=member
            )
            assert predicate.value == member.value


def test_a_typod_enum_literal_is_refused():
    """N2's own example. ``rail eq "card_emandat"`` used to load, pass its own
    allow/deny cases and never fire -- a rule set that reads as coverage the
    engine does not have."""
    with pytest.raises(ValidationError):
        FactPredicate(
            fact=PolicyFactKey.RAIL,
            operator=ComparisonOperator.EQ,
            value="card_emandat",
        )
    for fact in ENUM_FACT_VOCABULARIES:
        with pytest.raises(ValidationError):
            FactPredicate(
                fact=fact, operator=ComparisonOperator.EQ, value="not_a_member"
            )


def test_each_enum_fact_is_checked_against_its_own_enum_not_all_of_them():
    """A vocabulary assembled as the union of every enum would accept
    ``segment eq "card_emandate"`` -- a rule that reads plausibly and can never
    match a real fact value."""
    for fact, vocabulary in ENUM_FACT_VOCABULARIES.items():
        for other_fact, other in ENUM_FACT_VOCABULARIES.items():
            if other_fact is fact:
                continue
            foreign = {m.value for m in other} - {m.value for m in vocabulary}
            assert foreign, f"{fact.value} and {other_fact.value} share every value"
            with pytest.raises(ValidationError):
                FactPredicate(
                    fact=fact,
                    operator=ComparisonOperator.EQ,
                    value=sorted(foreign)[0],
                )


def test_enum_literals_are_checked_inside_an_in_tuple_too():
    """``in`` takes the same literals, and it is the natural way to write a
    multi-rail rule. Validating only the scalar form would leave the common case
    exactly as unchecked as it was before."""
    predicate = FactPredicate(
        fact=PolicyFactKey.RAIL,
        operator=ComparisonOperator.IN,
        value=("card_emandate", "upi_autopay"),
    )
    assert predicate.value == ("card_emandate", "upi_autopay")

    with pytest.raises(ValidationError):
        FactPredicate(
            fact=PolicyFactKey.RAIL,
            operator=ComparisonOperator.IN,
            value=("card_emandate", "card_emandat"),
        )


# ------------------------------------------------- N7: quiet-hours precedence


def test_a_payers_own_quiet_hours_win_and_carry_their_own_zone():
    """The payer stated a window; a configured default must not overrule it.
    Anything else is asking someone their hours and ignoring the answer."""
    profile = ConsentProfile(
        payer_id="payer_1",
        quiet_hours=QuietHours(
            start_hour_local=10, end_hour_local=17, timezone_name="America/New_York"
        ),
    )
    resolved = resolve_quiet_hours(profile, PolicyThresholds())
    assert resolved.start_hour_local == 10
    assert resolved.end_hour_local == 17
    assert resolved.timezone_name == "America/New_York"


def test_a_payer_with_no_stated_window_falls_back_to_the_config_in_ist():
    """The other branch. ``PolicyThresholds`` carries clock times and no zone at
    all, so the fallback has to name one; IST is named here and nowhere else."""
    profile = ConsentProfile(payer_id="payer_1")
    assert profile.quiet_hours is None

    resolved = resolve_quiet_hours(profile, PolicyThresholds())
    assert (resolved.start_hour_local, resolved.end_hour_local) == (9, 19)
    assert resolved.timezone_name == FALLBACK_QUIET_HOURS_TIMEZONE == "Asia/Kolkata"


def test_the_fallback_tracks_the_configured_window_rather_than_restating_it():
    """A fallback that hardcoded 09:00-19:00 would ignore the config it exists to
    read, and the two would drift the first time a threshold moved."""
    from datetime import time

    thresholds = PolicyThresholds(
        quiet_hours_start_local=time(8, 0), quiet_hours_end_local=time(20, 0)
    )
    resolved = resolve_quiet_hours(None, thresholds)
    assert (resolved.start_hour_local, resolved.end_hour_local) == (8, 20)


def test_no_profile_at_all_resolves_to_the_same_fallback():
    """§10.1 makes an unavailable profile a real state, and ``has_consent``
    already returns False for it. It is deliberately not a third precedence case:
    a DEFER rule still needs a window to wait for."""
    assert resolve_quiet_hours(None, PolicyThresholds()) == resolve_quiet_hours(
        ConsentProfile(payer_id="payer_1"), PolicyThresholds()
    )


def test_thresholds_reject_a_quiet_hours_boundary_that_is_not_on_the_hour():
    """``QuietHours`` expresses whole local hours, so a configured 09:30 would be
    truncated to 09:00 by the fallback and silently widen the window we may
    contact in. Refused at load rather than truncated at evaluation."""
    from datetime import time

    with pytest.raises(ValidationError):
        PolicyThresholds(quiet_hours_start_local=time(9, 30))
    with pytest.raises(ValidationError):
        PolicyThresholds(quiet_hours_end_local=time(19, 45))
