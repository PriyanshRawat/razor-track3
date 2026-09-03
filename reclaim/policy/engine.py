"""Evaluation: rules plus facts to one ``PolicyDecision``.

The engine is generic. It has no per-verb branch and no knowledge of what any
rule means; it reads ``ActionSpec`` to learn which categories apply and
``PolicyFactKey`` to learn what a predicate may test. That is what makes §14.4's
fourth defence ("the policy engine re-validates every proposal against state the
model cannot influence") structural: the model's proposal selects *which* rules
run, never *what* they see.

Three decisions in here are the ones worth arguing with.

**1. A declared category that returns no blocking verdict denies the action.**
``ActionSpec.policy_categories`` says these categories "MUST be evaluated before
this action". ``combine_verdicts`` already fails closed when *nothing* evaluated;
this is the same rule one level finer, because the dangerous case is not an empty
rule set -- it is a rule set that covers seven categories and silently drops the
eighth. ``Evaluation.silent_categories`` names the ones that said nothing, and
the decision is DENY with ``failed_closed=True``. The cost is real: the shipped
rule set must carry a permitting rule per category (``rules.py`` does this with
complementary DENY/``Not(DENY)`` pairs), and a partially-written rule set blocks
everything rather than allowing what it does cover. That is the intended
direction of failure.

**2. A rule whose facts are not all present is skipped, and recorded.** The
alternative -- three-valued logic that resolves what it can -- lets a deny rule
half-evaluate. Skipping the whole rule is stricter, and since a skipped rule
cannot cover its category, an unevaluable deny rule usually turns into a
fail-closed DENY rather than a silent allow. ``unevaluable_rule_ids`` is on the
result so it is visible; ``test_policy_facts.py`` asserts the shipped fact
builder leaves it empty for every action the router emits, which is what stops
"unevaluable" quietly becoming the normal case.

**3. An advisory rule cannot cover a category.** JC-21 says an advisory never
blocks. It must therefore never *permit* either, or a category could be signed
off by a check that has no veto power.

No clock, no I/O, no database. ``facts.py`` does the reading; this decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from reclaim.contracts.actions import ACTION_SPECS, ActionEnvelope, ActionType
from reclaim.contracts.enums import (
    Channel,
    PolicyCategory,
    PolicyEffect,
    RuleSeverity,
    Segment,
)
from reclaim.contracts.policy_format import (
    AllOf,
    AnyOf,
    ComparisonOperator,
    FactPredicate,
    Not,
    PolicyDecision,
    PolicyFactKey,
    PolicyRule,
    PolicyRuleSet,
    PolicyVerdict,
    combine_verdicts,
)

__all__ = [
    "Evaluation",
    "MissingFactError",
    "PolicyEngineError",
    "applicable_rules",
    "evaluate",
    "matches",
]


class PolicyEngineError(Exception):
    """Base class for evaluation failures that are programming errors."""


class MissingFactError(PolicyEngineError, KeyError):
    """A predicate tested a fact the bundle does not carry.

    Raised rather than answered False: a missing fact read as False silently
    disarms every deny rule written in the ``fact eq True`` form, which is the
    exact shape of most of §14.1's Holds row. ``evaluate`` catches this and
    skips the whole rule, so the raise is the internal signal, not the caller's
    problem.
    """

    def __init__(self, fact: PolicyFactKey) -> None:
        self.fact = fact
        super().__init__(
            f"policy fact {fact.value!r} is not in the bundle; a rule that tests "
            "it cannot be evaluated and must not be treated as satisfied"
        )


# ---------------------------------------------------------------- predicates


def _compare(operator: ComparisonOperator, actual: Any, expected: Any) -> bool:
    if operator is ComparisonOperator.EQ:
        return bool(actual == expected)
    if operator is ComparisonOperator.NE:
        return bool(actual != expected)
    if operator is ComparisonOperator.IN:
        return bool(actual in expected)
    if operator is ComparisonOperator.NOT_IN:
        return bool(actual not in expected)
    try:
        if operator is ComparisonOperator.GT:
            return bool(actual > expected)
        if operator is ComparisonOperator.GTE:
            return bool(actual >= expected)
        if operator is ComparisonOperator.LT:
            return bool(actual < expected)
        if operator is ComparisonOperator.LTE:
            return bool(actual <= expected)
    except TypeError as exc:  # pragma: no cover - guarded upstream by FactPredicate
        raise PolicyEngineError(
            f"cannot order {type(actual).__name__} against "
            f"{type(expected).__name__}: the fact bundle disagrees with the "
            "predicate's declared fact type"
        ) from exc
    raise PolicyEngineError(f"unhandled operator {operator!r}")  # pragma: no cover


def matches(predicate: Any, facts: Mapping[PolicyFactKey, Any]) -> bool:
    """Whether ``predicate`` holds over ``facts``.

    Raises ``MissingFactError`` for a fact the bundle does not carry. Two-valued
    on purpose: see the module docstring's point 2.
    """
    if isinstance(predicate, FactPredicate):
        if predicate.fact not in facts:
            raise MissingFactError(predicate.fact)
        return _compare(predicate.operator, facts[predicate.fact], predicate.value)
    if isinstance(predicate, AllOf):
        return all(matches(child, facts) for child in predicate.all_of)
    if isinstance(predicate, AnyOf):
        return any(matches(child, facts) for child in predicate.any_of)
    if isinstance(predicate, Not):
        return not matches(predicate.negate, facts)
    raise PolicyEngineError(  # pragma: no cover - Predicate is a closed union
        f"not a predicate node: {type(predicate).__name__}"
    )


# ------------------------------------------------------------ applicability


def _channel_of(envelope: ActionEnvelope) -> Channel | None:
    """The channel this action contacts on, read through the *declared* field name.

    ``ActionSpec.channel_field`` is the single place §14.1's consent, quiet-hours
    and frequency gates learn where the channel lives, and an import-time guard in
    ``actions.py`` checks the name resolves. Reading it any other way would put a
    second answer next to that one (review §6, finding 1).
    """
    field = ACTION_SPECS[envelope.action.action_type].channel_field
    return None if field is None else getattr(envelope.action, field)


def applicable_rules(
    rule_set: PolicyRuleSet,
    action_type: ActionType,
    *,
    segment: Segment,
    channel: Channel | None,
) -> tuple[PolicyRule, ...]:
    """The enabled rules whose category, segment and channel scope this action.

    A channel-scoped rule never applies to an action with no channel: a debit is
    authorised by a mandate, not by a contact channel, and letting a
    channel-scoped rule reach it would mean scoping on a field the action does
    not have.
    """
    categories = ACTION_SPECS[action_type].policy_categories
    selected: list[PolicyRule] = []
    for rule in rule_set.rules:
        if not rule.enabled or rule.category not in categories:
            continue
        if rule.applies_to_segments and segment not in rule.applies_to_segments:
            continue
        if rule.applies_to_channels and (
            channel is None or channel not in rule.applies_to_channels
        ):
            continue
        selected.append(rule)
    return tuple(selected)


# ------------------------------------------------------------------ evaluate


@dataclass(frozen=True)
class Evaluation:
    """One policy evaluation, with the bookkeeping the decision itself cannot hold.

    ``decision`` is the frozen contract type that goes on the audit row.
    Everything else records *how* the engine got there, which is what a reviewer
    needs when the answer is a fail-closed DENY with no deciding rule.
    """

    action_type: ActionType
    decision: PolicyDecision
    required_categories: tuple[PolicyCategory, ...]
    silent_categories: tuple[PolicyCategory, ...]
    unevaluable_rule_ids: tuple[str, ...]
    matched_rule_ids: tuple[str, ...]

    @property
    def effect(self) -> PolicyEffect:
        return self.decision.effect

    @property
    def is_allowed(self) -> bool:
        return self.decision.is_allowed

    @property
    def deciding_rule_id(self) -> str | None:
        return self.decision.deciding_rule_id

    def explain(self) -> str:
        """One line a human can read on the receipt (§14.1: DENY carries a reason)."""
        if self.silent_categories:
            names = ", ".join(c.value for c in self.silent_categories)
            return f"failed closed: no rule evaluated for category {names}"
        deciding = self.decision.deciding_rule_id
        if deciding is None:  # pragma: no cover - only reachable via failed_closed
            return "failed closed: nothing evaluated"
        for verdict in self.decision.verdicts:
            if verdict.rule_id == deciding:
                reason = verdict.human_reason.strip()
                return f"{deciding}: {reason}" if reason else deciding
        return deciding  # pragma: no cover - deciding is always among the verdicts


def evaluate(
    rule_set: PolicyRuleSet,
    envelope: ActionEnvelope,
    facts: Mapping[PolicyFactKey, Any],
    *,
    segment: Segment,
) -> Evaluation:
    """Decide whether ``envelope`` may proceed, given ``facts``.

    Every verdict is retained, allows included (§14.1) -- the Recovery Receipt
    shows what policy allowed as well as what it denied.
    """
    action_type = envelope.action.action_type
    spec = ACTION_SPECS[action_type]
    required = tuple(sorted(spec.policy_categories, key=lambda c: c.value))

    verdicts: list[PolicyVerdict] = []
    unevaluable: list[str] = []
    matched: list[str] = []

    for rule in applicable_rules(
        rule_set, action_type, segment=segment, channel=_channel_of(envelope)
    ):
        needed = set(rule.referenced_facts)
        if rule.defer_until_fact is not None:
            needed.add(rule.defer_until_fact)
        if not needed <= set(facts):
            unevaluable.append(rule.rule_id)
            continue
        if not matches(rule.when, facts):
            continue
        matched.append(rule.rule_id)
        verdicts.append(
            PolicyVerdict(
                rule_id=rule.rule_id,
                category=rule.category,
                effect=rule.effect,
                human_reason=rule.human_reason,
                severity=rule.severity,
                requires_tier=rule.requires_tier,
                defer_until=(
                    facts[rule.defer_until_fact]
                    if rule.effect is PolicyEffect.DEFER
                    and rule.defer_until_fact is not None
                    else None
                ),
            )
        )

    decision = combine_verdicts(verdicts, rule_set.policy_version)
    silent = _silent_categories(required, verdicts)
    if silent:
        decision = _fail_closed(decision, rule_set.policy_version)

    return Evaluation(
        action_type=action_type,
        decision=decision,
        required_categories=required,
        silent_categories=silent,
        unevaluable_rule_ids=tuple(unevaluable),
        matched_rule_ids=tuple(matched),
    )


def _silent_categories(
    required: Sequence[PolicyCategory], verdicts: Sequence[PolicyVerdict]
) -> tuple[PolicyCategory, ...]:
    """Declared categories that produced no *blocking* verdict (JC-21)."""
    covered = {v.category for v in verdicts if v.severity is RuleSeverity.BLOCKING}
    return tuple(c for c in required if c not in covered)


def _fail_closed(decision: PolicyDecision, policy_version: str) -> PolicyDecision:
    """Rebuild ``decision`` as a fail-closed DENY, keeping every verdict.

    Constructed field by field rather than by ``model_copy``: the repo's rule is
    that a model change goes through validation, and a decision assembled by
    copy-with-update is one that no validator ever saw.
    """
    return PolicyDecision(
        effect=PolicyEffect.DENY,
        deciding_rule_id=None,
        verdicts=decision.verdicts,
        advisory_rule_ids=decision.advisory_rule_ids,
        requires_tier=decision.requires_tier,
        defer_until=None,
        failed_closed=True,
        policy_version=policy_version,
    )
