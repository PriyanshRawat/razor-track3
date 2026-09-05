"""The simulator's calibration anchors (§11.2).

Every number in ``reclaim.sim.anchors`` is an **assumption**, not a measurement --
§11.2 is explicit that no public labelled dataset of decline-code -> intervention ->
outcome exists, so the environment is calibrated, not observed. These tests do not
check that the numbers are *right* (nothing here can); they check the three things
that make the table safe to reason about:

* it is **total** -- a test walks every ``DeclineClass`` and every ``RiskClass``,
  because a mapping asserted by eye is not frozen (four of Phase 0's five review
  defects lived in exactly that shape);
* it is **shaped like the plan's causal claims** -- §9.2 H3 says a retry on a dead
  mandate is 0% and still costs a fee, so the table must give a debit retry on a
  dead-mandate class exactly zero credit, and §9.2 H1 says NSF is the class where
  re-timing the debit *is* the intervention;
* the arms are **ordered by construction** -- A1's uplift is one undifferentiated
  number for every class (that is what makes it a naive baseline), and it is
  smaller than A4's uplift wherever A4 picks the verb §9.2 prescribes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from reclaim.contracts.actions import ActionType
from reclaim.contracts.decline_taxonomy import (
    DECLINE_CLASS_META,
    DeclineClass,
    Retryability,
)
from reclaim.contracts.enums import RiskClass
from reclaim.contracts.units import PROBABILITY_SCALE
from reclaim.sim import anchors


def _is_six_dp(value: Decimal) -> bool:
    return -value.as_tuple().exponent == PROBABILITY_SCALE


# ------------------------------------------------------------------ totality


@pytest.mark.parametrize("decline_class", list(DeclineClass))
def test_every_decline_class_has_a_recovery_anchor(decline_class):
    assert decline_class in anchors.DECLINE_ANCHORS


@pytest.mark.parametrize("risk_class", list(RiskClass))
def test_every_risk_class_has_a_fallback_anchor(risk_class):
    """A D3 case carries no decline class at all (JC-42), so the risk class is the
    only key the simulator has for it. A missing entry would silently fall back to
    someone else's favourite default."""
    assert risk_class in anchors.RISK_CLASS_ANCHORS


def test_the_two_tables_together_cover_every_anchor_the_resolver_can_ask_for():
    assert set(anchors.DECLINE_ANCHORS) == set(DeclineClass)
    assert set(anchors.RISK_CLASS_ANCHORS) == set(RiskClass)


# ------------------------------------------------------- numeric well-formedness


def test_every_anchor_is_a_six_dp_probability_inside_the_unit_interval():
    for key, anchor in anchors.all_anchors().items():
        for name, value in (
            ("natural", anchor.natural),
            ("uplift_correct", anchor.uplift_correct),
            ("uplift_wrong", anchor.uplift_wrong),
        ):
            assert isinstance(value, Decimal), f"{key}.{name} is not a Decimal"
            assert _is_six_dp(value), f"{key}.{name} is not at {PROBABILITY_SCALE} dp"
            assert Decimal(0) <= value <= Decimal(1), f"{key}.{name} out of range"
        assert anchor.natural + anchor.uplift_correct <= Decimal(1), key


def test_every_band_brackets_its_own_point_estimate():
    """§11.2 requires each anchor to carry an uncertainty band. A band that does not
    contain the number it qualifies is a typo that would survive every other test."""
    for key, anchor in anchors.all_anchors().items():
        low, high = anchor.natural_band
        assert low <= anchor.natural <= high, f"{key}: {low} <= {anchor.natural} <= {high}"
        assert low < high, f"{key}: band is a point, not a range"


def test_every_anchor_names_the_assumption_behind_it():
    for key, anchor in anchors.all_anchors().items():
        assert anchor.source.strip(), f"{key} has no stated source"


def test_the_module_carries_the_honesty_statement_the_plan_puts_on_a_slide():
    note = anchors.ANCHOR_HONESTY_NOTE.lower()
    assert "assumption" in note
    assert "not" in note and "real" in note


# --------------------------------------------------- the plan's causal claims


_DEAD_MANDATE_CLASSES = tuple(
    c
    for c, meta in DECLINE_CLASS_META.items()
    if meta.retryability is Retryability.REQUIRES_NEW_MANDATE
)


def test_the_dead_mandate_classes_are_not_an_empty_set():
    """Guards the test below: if the taxonomy stopped using REQUIRES_NEW_MANDATE the
    H3 assertion would pass by vacuity."""
    assert len(_DEAD_MANDATE_CLASSES) >= 4


@pytest.mark.parametrize("decline_class", _DEAD_MANDATE_CLASSES)
def test_a_debit_retry_on_a_dead_mandate_earns_exactly_nothing(decline_class):
    """§9.2 H3: "a retry is 0% and costs a failed-attempt fee". The anchor must give
    the wrong verb *zero* uplift, not a small one -- a small one lets a retry engine
    look like a recovery engine."""
    anchor = anchors.DECLINE_ANCHORS[decline_class]
    assert anchor.correct_verb is not ActionType.SCHEDULE_DEBIT
    assert anchor.uplift_wrong == Decimal("0.000000")
    assert (
        anchors.targeted_probability(decline_class, None, ActionType.SCHEDULE_DEBIT)
        == anchor.natural
    )


def test_a_mandate_reauth_journey_is_the_verb_that_earns_the_uplift():
    for decline_class in _DEAD_MANDATE_CLASSES:
        anchor = anchors.DECLINE_ANCHORS[decline_class]
        assert anchor.correct_verb is ActionType.SEND_MESSAGE
        assert anchor.uplift_correct > Decimal(0)


def test_nsf_is_the_class_where_re_timing_the_debit_is_the_intervention():
    """§9.2 H1: timing against the payer's liquidity calendar is the whole action,
    and no contact is usually correct."""
    anchor = anchors.DECLINE_ANCHORS[DeclineClass.INSUFFICIENT_FUNDS]
    assert anchor.correct_verb is ActionType.SCHEDULE_DEBIT
    assert anchor.uplift_correct > anchor.uplift_wrong
    assert anchor.uplift_correct >= Decimal("0.100000")


# ------------------------------------------------------------ arm ordering


def test_the_naive_baseline_uplift_is_one_number_for_every_decline_reason():
    """A1 is "always take the same generic action" (§12.2). If its uplift varied by
    class it would be smuggling in the intervention choice that A3/A4 isolate."""
    uplifts = {
        c: anchors.generic_probability(c, None) - anchors.DECLINE_ANCHORS[c].natural
        for c in DeclineClass
        if anchors.DECLINE_ANCHORS[c].natural + anchors.GENERIC_TOTAL_UPLIFT
        <= anchors.RECOVERY_PROBABILITY_CEILING
    }
    assert len(set(uplifts.values())) == 1
    assert next(iter(uplifts.values())) == anchors.GENERIC_TOTAL_UPLIFT


def test_the_generic_total_uplift_matches_its_documented_recipe():
    """Re-derived here rather than read back: a decaying 4-touch static drip. Asking
    the module for its own answer would not catch a changed decay."""
    expected = sum(
        (
            anchors.GENERIC_UPLIFT_FIRST_TOUCH * anchors.GENERIC_TOUCH_DECAY**i
            for i in range(anchors.GENERIC_TOUCH_COUNT)
        ),
        Decimal(0),
    )
    assert anchors.GENERIC_TOTAL_UPLIFT == anchors.probability(expected)


def test_the_agent_beats_the_naive_baseline_wherever_it_picks_the_right_verb():
    better = 0
    for decline_class, anchor in anchors.DECLINE_ANCHORS.items():
        if anchor.correct_verb is None:
            continue
        targeted = anchors.targeted_probability(
            decline_class, None, anchor.correct_verb
        )
        generic = anchors.generic_probability(decline_class, None)
        assert targeted >= generic, decline_class
        better += targeted > generic
    assert better >= 10


def test_no_probability_the_anchors_can_produce_exceeds_the_ceiling():
    for decline_class in DeclineClass:
        for verb in (ActionType.SCHEDULE_DEBIT, ActionType.SEND_MESSAGE, None):
            assert (
                anchors.targeted_probability(decline_class, None, verb)
                <= anchors.RECOVERY_PROBABILITY_CEILING
            )
        assert (
            anchors.generic_probability(decline_class, None)
            <= anchors.RECOVERY_PROBABILITY_CEILING
        )
        assert (
            anchors.natural_probability(decline_class, None)
            <= anchors.RECOVERY_PROBABILITY_CEILING
        )


def test_a_case_with_no_decline_class_falls_back_to_its_risk_class():
    overdue = anchors.RISK_CLASS_ANCHORS[RiskClass.OVERDUE_RECEIVABLE]
    assert (
        anchors.natural_probability(None, RiskClass.OVERDUE_RECEIVABLE)
        == overdue.natural
    )
    assert (
        anchors.natural_probability(
            DeclineClass.INSUFFICIENT_FUNDS, RiskClass.OVERDUE_RECEIVABLE
        )
        == anchors.DECLINE_ANCHORS[DeclineClass.INSUFFICIENT_FUNDS].natural
    )


def test_asking_for_no_key_at_all_is_a_programming_error_not_a_default():
    with pytest.raises(ValueError):
        anchors.natural_probability(None, None)


def _split(key):
    """``all_anchors`` merges two disjoint enums; ``targeted_probability``
    takes them on separate parameters."""
    if isinstance(key, DeclineClass):
        return key, None
    return None, key


# ------------------------------------------------------------- the hedge share
#
# ``flow.hedged_route`` lets a case whose diagnosis is *contested* send one
# non-committal contact instead of escalating into a queue with no consumer. The
# simulator must not pay for that contact as though the cause had been resolved,
# or the coverage it buys reads as diagnostic skill.


def test_a_hedged_contact_earns_less_than_a_correctly_targeted_one():
    """The whole point. If these were equal, routing the right message and
    shrugging would score the same and A4's number would stop measuring
    diagnosis."""
    for key, anchor in anchors.all_anchors().items():
        if anchor.correct_verb is None or anchor.uplift_correct == 0:
            continue
        decline, risk = _split(key)
        targeted = anchors.targeted_probability(decline, risk, anchor.correct_verb)
        hedged = anchors.targeted_probability(
            decline, risk, anchor.correct_verb, hedged=True
        )
        assert hedged < targeted, key


def test_a_hedged_contact_still_earns_more_than_no_contact_at_all():
    """The other side. A hedge that scored the natural rate would make the
    fallback invisible in the numbers, which is just as dishonest in the other
    direction -- a real message with a working link does reach the payer."""
    for key, anchor in anchors.all_anchors().items():
        if anchor.correct_verb is None or anchor.uplift_correct == 0:
            continue
        decline, risk = _split(key)
        hedged = anchors.targeted_probability(
            decline, risk, anchor.correct_verb, hedged=True
        )
        assert hedged > anchors.natural_probability(decline, risk), key


def test_a_hedged_wrong_verb_is_still_a_wrong_verb():
    """The discount applies to the *correct* verb sent without resolving the
    cause. It must not become a way for a mistargeted action to earn something:
    a hedged debit on a dead mandate is still worth exactly zero."""
    for key, anchor in anchors.all_anchors().items():
        if anchor.correct_verb is not ActionType.SEND_MESSAGE:
            continue
        decline, risk = _split(key)
        wrong = ActionType.SCHEDULE_DEBIT
        assert anchors.targeted_probability(
            decline, risk, wrong, hedged=True
        ) == anchors.targeted_probability(decline, risk, wrong), key


def test_the_hedge_share_is_a_stated_assumption_inside_its_own_band():
    """§11.2's convention: an invented number carries the band it was invented
    within, and the band is what a sensitivity run sweeps."""
    low, high = anchors.HEDGED_UPLIFT_SHARE_BAND
    assert Decimal("0") < low <= anchors.HEDGED_UPLIFT_SHARE <= high < Decimal("1")


def test_the_hedge_share_note_says_it_is_an_assumption():
    assert "assum" in anchors.ANCHOR_HONESTY_NOTE.lower()
