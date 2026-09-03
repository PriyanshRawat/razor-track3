"""Frozen version constants.

Every decision recorded in the audit log carries these so that a metric shift can
be attributed to a specific change (§15 "Model/prompt/policy/versioning").

Bump rules
----------
* PATCH -- docstring/comment only.
* MINOR -- additive: a new enum member, a new optional field, a new action type.
* MAJOR -- breaking: rename/remove a field or enum member, change a semantic.

A MAJOR bump on ``ACTION_CATALOG_VERSION`` or ``POLICY_FORMAT_VERSION`` after the
``SEED_EVAL`` run invalidates the reported scoreboard (§11.4).
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ACTION_CATALOG_VERSION",
    "ARM_ASSIGNMENT_VERSION",
    "AUDIT_SCHEMA_VERSION",
    "CONTRACTS_SCHEMA_VERSION",
    "DECLINE_TAXONOMY_VERSION",
    "EVENT_SCHEMA_VERSION",
    "METRICS_VERSION",
    "POLICY_FORMAT_VERSION",
    "PHASE_0_FROZEN",
]

#: Umbrella version for everything under ``reclaim.contracts``.
#: 1.1.0 -- additive: ``RiskCase.canonical_decline_class`` (JC-42 / CONTRACTS.md
#: Q10). No field was renamed or removed, so a 1.0.0 payload still parses.
#: 2.0.0 -- **breaking**: three free-string fields became closed vocabularies
#: (§7 N3/N4/N5). ``Hold.kind`` changes value as well as type -- ``'opt_out'``
#: is now ``'hard_stop_opt_out'`` -- and ``PartialPayment.match_method`` and
#: ``ConsentProfile`` now reject payloads 1.x accepted. Nothing persisted
#: predates this, but the rule is the rule: narrowing a field is a MAJOR bump.
#: 3.0.0 -- **breaking**: the policy-engine amendment (§7 N1/N2/N6/N7).
#: ``ConsentProfile.quiet_hours`` is now ``QuietHours | None`` defaulting to
#: ``None``, which is the one shape of break that does not announce itself: a 2.x
#: payload omitting the field still parses, to a *different value*. Code that read
#: ``profile.quiet_hours.start_hour_local`` must go through
#: ``policy_format.resolve_quiet_hours`` (JC-43). ``PolicyThresholds`` and
#: ``FactPredicate`` additionally reject configs and predicates 2.x accepted.
CONTRACTS_SCHEMA_VERSION: Final[str] = "3.0.0"

EVENT_SCHEMA_VERSION: Final[str] = "1.0.0"
DECLINE_TAXONOMY_VERSION: Final[str] = "1.0.0"
#: 1.1.0 -- additive: ``schedule_debit`` now also asks for the financial-authority
#: rule category (§7 N6). Strictly more policy is evaluated, never less, and no
#: action model changed a field -- the idempotency scope is ``{case_id, params}``
#: and does not carry this constant, so a 1.0.0 ``ActionEnvelope`` still parses and
#: still derives the same key.
ACTION_CATALOG_VERSION: Final[str] = "1.1.0"

#: 2.0.0 -- **breaking**: a rule set 1.0.0 would have loaded may now fail to load.
#: ``FactPredicate`` gained the TIMESTAMP and ENUM branches it never had (§7 N2),
#: so ``quiet_hours_end_at gte 5`` and ``rail eq "card_emandat"`` are load errors
#: instead of rules that read correctly and never fire; ``PolicyThresholds`` now
#: requires a grace-period entry for every segment (§7 N1) and whole-hour
#: quiet-hours boundaries (JC-43). Everything newly rejected is something that
#: could not have worked -- but the bump is MAJOR because that is what narrowing
#: is, and because ``PolicyRuleSet.format_version`` defaults to this constant.
#: Safe to do now and not later: the §11.4 ``SEED_EVAL`` run has not happened, so
#: no reported scoreboard depends on either of these two versions yet.
POLICY_FORMAT_VERSION: Final[str] = "2.0.0"
AUDIT_SCHEMA_VERSION: Final[str] = "1.0.0"
METRICS_VERSION: Final[str] = "1.0.0"
ARM_ASSIGNMENT_VERSION: Final[str] = "1.0.0"

#: Set to True when Phase 0 is reviewed and merged. Phase 1 code may assert this.
PHASE_0_FROZEN: Final[bool] = False
