"""Phase 1: the policy engine's evaluation semantics.

``policy_format.py`` froze the *shape* of a rule and the *lattice* that combines
verdicts; it deliberately shipped no evaluator. This file pins the evaluator --
the part that decides which rules an action is subject to, what a missing fact
means, and when the whole thing refuses to answer.

Three properties carry the weight, and each is a way the engine could fail *open*:

1. **Category coverage.** ``ActionSpec.policy_categories`` says which rule
   categories "MUST be evaluated before this action". A category that is declared
   but produces no blocking verdict is not a category that approved the action --
   it is one nobody asked. The engine denies, and names it.
2. **A rule it cannot evaluate is not a rule that passed.** If the fact bundle is
   missing something the rule tests, the rule is skipped *and recorded*, so a
   silently dead deny rule shows up as an ``unevaluable_rule_id`` rather than as
   an allow.
3. **Order independence.** JC-20 makes effects a lattice precisely so rule order
   cannot decide anything. The reversal test is what makes that true of the
   evaluator and not just of ``combine_verdicts``.

The rule sets here are hand-built and deliberately trivial: this file is about the
engine, not about the shipped rules (``test_policy_rules.py``) or about the facts
(``test_policy_facts.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reclaim.contracts.actions import (
    ACTION_SPECS,
    ActionEnvelope,
    ActionType,
    ScheduleDebit,
    SendMessage,
)
from reclaim.contracts.enums import (
    AutonomyTier,
    Channel,
    Language,
    MessageIntent,
    PlanOrigin,
    PolicyCategory,
    PolicyEffect,
    Rail,
    RuleSeverity,
    Segment,
)
from reclaim.contracts.money import Money
from reclaim.contracts.policy_format import (
    AllOf,
    AnyOf,
    ComparisonOperator as Op,
    FactPredicate,
    Not,
    PolicyFactKey as F,
    PolicyRule,
    PolicyRuleSet,
    RuleTestCase,
)
from reclaim.policy.engine import MissingFactError, evaluate, matches

TS = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)

#: A predicate that is true in the baseline bundle below, and its inverse. Used
#: wherever a test needs "a rule that fires" without caring what it says.
TRUE_WHEN = FactPredicate(fact=F.IS_OPTED_OUT, operator=Op.EQ, value=False)
FALSE_WHEN = FactPredicate(fact=F.IS_OPTED_OUT, operator=Op.EQ, value=True)

BASELINE_FACTS = {F.IS_OPTED_OUT: False}


def _rule(rule_id, category, effect, when=TRUE_WHEN, **over):
    fields = dict(
        rule_id=rule_id,
        category=category,
        title=f"{rule_id} test rule",
        effect=effect,
        when=when,
        human_reason="test rule",
        citation="tests/test_policy_engine.py",
        test_cases=(
            RuleTestCase(
                name="matching", facts={F.IS_OPTED_OUT: False}, expect_matches=True
            ),
            RuleTestCase(
                name="non-matching", facts={F.IS_OPTED_OUT: True}, expect_matches=False
            ),
        ),
    )
    fields.update(over)
    return PolicyRule(**fields)


def _covering_rules(action_type: ActionType, effect=PolicyEffect.ALLOW):
    """One matching rule per category the action declares -- the minimum a rule
    set needs before the engine will consider the action at all."""
    categories = sorted(
        ACTION_SPECS[action_type].policy_categories, key=lambda c: c.value
    )
    return [
        _rule(f"POL-BASE-{i:03d}", category, effect)
        for i, category in enumerate(categories)
    ]


def _rule_set(rules, version="1.0.0"):
    return PolicyRuleSet(policy_version=version, rules=tuple(rules))


def _debit(**over):
    action = ScheduleDebit(
        obligation_id=over.pop("obligation_id", "obl_1"),
        rail=over.pop("rail", Rail.CARD_ONE_TIME),
        amount=over.pop("amount", Money.from_rupees(1499)),
        execute_at=over.pop("execute_at", TS + timedelta(days=1)),
        attempt_sequence=over.pop("attempt_sequence", 2),
    )
    return ActionEnvelope(
        action_id=over.pop("action_id", "act_1"),
        case_id=over.pop("case_id", "case_1"),
        action=action,
        proposed_by=PlanOrigin.DETERMINISTIC_ROUTER,
    )


def _message(**over):
    action = SendMessage(
        channel=over.pop("channel", Channel.WHATSAPP),
        template_id=over.pop("template_id", "tpl_test"),
        language=over.pop("language", Language.EN_IN),
        intent=over.pop("intent", MessageIntent.PAYMENT_REMINDER),
    )
    return ActionEnvelope(
        action_id=over.pop("action_id", "act_1"),
        case_id=over.pop("case_id", "case_1"),
        action=action,
        proposed_by=PlanOrigin.DETERMINISTIC_ROUTER,
    )


# ---------------------------------------------------------------- predicates


def test_matches_reads_a_boolean_fact():
    assert matches(TRUE_WHEN, {F.IS_OPTED_OUT: False}) is True
    assert matches(TRUE_WHEN, {F.IS_OPTED_OUT: True}) is False


def test_matches_orders_money_without_unit_confusion():
    when = FactPredicate(
        fact=F.DEBIT_AMOUNT, operator=Op.GT, value=Money.from_rupees(15000)
    )
    assert matches(when, {F.DEBIT_AMOUNT: Money.from_rupees(15001)}) is True
    assert matches(when, {F.DEBIT_AMOUNT: Money.from_rupees(15000)}) is False


def test_matches_orders_timestamps():
    when = FactPredicate(fact=F.QUIET_HOURS_END_AT, operator=Op.LTE, value=TS)
    assert matches(when, {F.QUIET_HOURS_END_AT: TS - timedelta(hours=1)}) is True
    assert matches(when, {F.QUIET_HOURS_END_AT: TS + timedelta(hours=1)}) is False


def test_matches_handles_membership():
    when = FactPredicate(fact=F.RAIL, operator=Op.IN, value=("card_emandate", "enach"))
    assert matches(when, {F.RAIL: "enach"}) is True
    assert matches(when, {F.RAIL: "card_one_time"}) is False


def test_matches_composes_all_of_any_of_and_not():
    both = AllOf(all_of=(TRUE_WHEN, FALSE_WHEN))
    either = AnyOf(any_of=(TRUE_WHEN, FALSE_WHEN))
    assert matches(both, BASELINE_FACTS) is False
    assert matches(either, BASELINE_FACTS) is True
    assert matches(Not(negate=either), BASELINE_FACTS) is False


def test_matches_refuses_to_guess_at_a_missing_fact():
    """A missing fact is not False. Treating it as False silently disarms every
    deny rule written in the ``fact eq True`` form."""
    with pytest.raises(MissingFactError):
        matches(TRUE_WHEN, {})


# ------------------------------------------------------------------ verdicts


def test_an_action_every_required_category_allows_is_allowed():
    result = evaluate(
        _rule_set(_covering_rules(ActionType.SCHEDULE_DEBIT)),
        _debit(),
        BASELINE_FACTS,
        segment=Segment.B2C_STANDARD,
    )
    assert result.decision.effect is PolicyEffect.ALLOW
    assert result.silent_categories == ()
    assert result.decision.failed_closed is False


def test_a_matching_deny_decides_and_names_its_rule():
    rules = _covering_rules(ActionType.SCHEDULE_DEBIT) + [
        _rule("POL-FIN-001", PolicyCategory.FINANCIAL_AUTHORITY, PolicyEffect.DENY)
    ]
    result = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.DENY
    assert result.decision.deciding_rule_id == "POL-FIN-001"
    assert result.decision.failed_closed is False


def test_the_decision_does_not_depend_on_rule_order():
    rules = _covering_rules(ActionType.SCHEDULE_DEBIT) + [
        _rule("POL-FIN-001", PolicyCategory.FINANCIAL_AUTHORITY, PolicyEffect.DENY),
        _rule("POL-HOLD-001", PolicyCategory.HOLDS, PolicyEffect.DENY),
    ]
    forwards = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    backwards = evaluate(
        _rule_set(list(reversed(rules))),
        _debit(),
        BASELINE_FACTS,
        segment=Segment.B2C_STANDARD,
    )
    assert forwards.decision.effect is backwards.decision.effect is PolicyEffect.DENY
    assert forwards.decision.deciding_rule_id == backwards.decision.deciding_rule_id


def test_a_declared_category_with_no_rule_fails_closed_and_is_named():
    """The whole point of ``policy_categories``. A category nobody wrote a rule
    for has not approved anything -- and the engine must say which one."""
    rules = [
        r
        for r in _covering_rules(ActionType.SCHEDULE_DEBIT)
        if r.category is not PolicyCategory.HOLDS
    ]
    result = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.DENY
    assert result.decision.failed_closed is True
    assert result.decision.deciding_rule_id is None
    assert result.silent_categories == (PolicyCategory.HOLDS,)


def test_a_rule_whose_facts_are_missing_is_recorded_not_passed():
    """An unevaluable deny rule must never read as an allow. Here it is the only
    rule in its category, so the action is denied and the rule is named."""
    unreachable = _rule(
        "POL-HOLD-001",
        PolicyCategory.HOLDS,
        PolicyEffect.DENY,
        when=FactPredicate(fact=F.HAS_LEGAL_HOLD, operator=Op.EQ, value=True),
    )
    rules = [
        r
        for r in _covering_rules(ActionType.SCHEDULE_DEBIT)
        if r.category is not PolicyCategory.HOLDS
    ] + [unreachable]
    result = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert result.unevaluable_rule_ids == ("POL-HOLD-001",)
    assert result.decision.failed_closed is True
    assert result.silent_categories == (PolicyCategory.HOLDS,)


def test_a_rule_outside_the_actions_categories_is_not_evaluated():
    """``send_message`` declares no financial-authority category, so a matching
    deny there must not touch it."""
    rules = _covering_rules(ActionType.SEND_MESSAGE) + [
        _rule("POL-FIN-001", PolicyCategory.FINANCIAL_AUTHORITY, PolicyEffect.DENY)
    ]
    result = evaluate(
        _rule_set(rules), _message(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.ALLOW
    assert "POL-FIN-001" not in [v.rule_id for v in result.decision.verdicts]


def test_a_disabled_rule_is_not_evaluated():
    rules = _covering_rules(ActionType.SCHEDULE_DEBIT) + [
        _rule(
            "POL-FIN-001",
            PolicyCategory.FINANCIAL_AUTHORITY,
            PolicyEffect.DENY,
            enabled=False,
        )
    ]
    result = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.ALLOW


def test_a_segment_scoped_rule_only_fires_in_its_segment():
    rules = _covering_rules(ActionType.SCHEDULE_DEBIT) + [
        _rule(
            "POL-FIN-001",
            PolicyCategory.FINANCIAL_AUTHORITY,
            PolicyEffect.DENY,
            applies_to_segments=(Segment.B2B_ENTERPRISE,),
        )
    ]
    inside = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2B_ENTERPRISE
    )
    outside = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert inside.decision.effect is PolicyEffect.DENY
    assert outside.decision.effect is PolicyEffect.ALLOW


def test_a_channel_scoped_rule_reads_the_channel_off_the_action():
    """§14.1's gates find the channel through ``ActionSpec.channel_field`` -- the
    same declared name the import-time guard in ``actions.py`` protects."""
    rules = _covering_rules(ActionType.SEND_MESSAGE) + [
        _rule(
            "POL-CONTENT-001",
            PolicyCategory.CONTENT,
            PolicyEffect.DENY,
            applies_to_channels=(Channel.SMS,),
        )
    ]
    on_sms = evaluate(
        _rule_set(rules),
        _message(channel=Channel.SMS),
        BASELINE_FACTS,
        segment=Segment.B2C_STANDARD,
    )
    on_whatsapp = evaluate(
        _rule_set(rules),
        _message(channel=Channel.WHATSAPP),
        BASELINE_FACTS,
        segment=Segment.B2C_STANDARD,
    )
    assert on_sms.decision.effect is PolicyEffect.DENY
    assert on_whatsapp.decision.effect is PolicyEffect.ALLOW


def test_a_channel_scoped_rule_never_applies_to_an_action_with_no_channel():
    """A debit is authorised by a mandate, not by a channel. A channel-scoped rule
    reaching it would be reading a field the action does not have."""
    rules = _covering_rules(ActionType.SCHEDULE_DEBIT) + [
        _rule(
            "POL-HOLD-002",
            PolicyCategory.HOLDS,
            PolicyEffect.DENY,
            applies_to_channels=(Channel.SMS,),
        )
    ]
    result = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.ALLOW


# ------------------------------------------------------- severity and effects


def test_an_advisory_verdict_is_recorded_but_never_blocks():
    rules = _covering_rules(ActionType.SCHEDULE_DEBIT) + [
        _rule(
            "POL-FIN-001",
            PolicyCategory.FINANCIAL_AUTHORITY,
            PolicyEffect.DENY,
            severity=RuleSeverity.ADVISORY,
        )
    ]
    result = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.ALLOW
    assert result.decision.advisory_rule_ids == ("POL-FIN-001",)


def test_an_advisory_rule_alone_does_not_cover_its_category():
    """JC-21 says an advisory never blocks; it must therefore never *permit*
    either, or a category could be signed off by a check with no veto power."""
    rules = [
        r
        for r in _covering_rules(ActionType.SCHEDULE_DEBIT)
        if r.category is not PolicyCategory.HOLDS
    ] + [
        _rule(
            "POL-HOLD-001",
            PolicyCategory.HOLDS,
            PolicyEffect.ALLOW,
            severity=RuleSeverity.ADVISORY,
        )
    ]
    result = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert result.decision.failed_closed is True
    assert result.silent_categories == (PolicyCategory.HOLDS,)


def test_allow_with_approval_is_allowed_and_carries_its_tier():
    rules = _covering_rules(ActionType.SCHEDULE_DEBIT) + [
        _rule(
            "POL-FIN-001",
            PolicyCategory.FINANCIAL_AUTHORITY,
            PolicyEffect.ALLOW_WITH_APPROVAL,
            requires_tier=AutonomyTier.T2,
        )
    ]
    result = evaluate(
        _rule_set(rules), _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.ALLOW_WITH_APPROVAL
    assert result.decision.requires_tier is AutonomyTier.T2
    assert result.decision.is_allowed is True


def test_defer_reads_its_instant_from_the_named_timestamp_fact():
    rules = _covering_rules(ActionType.SEND_MESSAGE) + [
        _rule(
            "POL-TIME-001",
            PolicyCategory.TIMING,
            PolicyEffect.DEFER,
            when=FactPredicate(
                fact=F.IS_WITHIN_QUIET_HOURS, operator=Op.EQ, value=False
            ),
            defer_until_fact=F.QUIET_HOURS_END_AT,
        )
    ]
    facts = dict(
        BASELINE_FACTS,
        **{F.IS_WITHIN_QUIET_HOURS: False, F.QUIET_HOURS_END_AT: TS},
    )
    result = evaluate(_rule_set(rules), _message(), facts, segment=Segment.B2C_STANDARD)
    assert result.decision.effect is PolicyEffect.DEFER
    assert result.decision.defer_until == TS


def test_the_decision_carries_the_rule_sets_version():
    rule_set = _rule_set(_covering_rules(ActionType.SCHEDULE_DEBIT), version="2.3.4")
    result = evaluate(rule_set, _debit(), BASELINE_FACTS, segment=Segment.B2C_STANDARD)
    assert result.decision.policy_version == "2.3.4"
