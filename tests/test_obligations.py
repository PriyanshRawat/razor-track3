"""Contract tests for the consent, hold and payment-matching half of ``obligations.py``.

This half of the module had **no test coverage at all** through Phase 0, which is
why all three defects fixed here (CONTRACTS.md §7 N3/N4/N5) survived 322 green
tests. Each was the same shape: a field description naming a closed vocabulary in
prose while the annotation stayed ``str``.

The tests below therefore walk the vocabularies rather than spot-checking a member,
per CLAUDE.md -- "a table is not frozen because it reads correctly; it is frozen
when a test walks every row" -- and the two that pair a ledger model with its event
re-derive the event's vocabulary instead of restating it, so drift between the two
fails here rather than in a Phase 1 mapper.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from reclaim.contracts.enums import Channel, HOLD_STOP_REASONS, StopReason
from reclaim.contracts.events import PaymentReceivedPayload
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import (
    ConsentProfile,
    ConsentRecord,
    Hold,
    PartialPayment,
    QuietHours,
    has_consent,
)

_T0 = datetime(2026, 3, 1, 4, 30, tzinfo=timezone.utc)


def _record(**kw) -> ConsentRecord:
    kwargs = dict(
        channel=Channel.EMAIL,
        granted=True,
        source="signup_form_v3",
        dpdp_purpose="payment_recovery",
    )
    kwargs.update(kw)
    return ConsentRecord(**kwargs)


def _hold(**kw) -> Hold:
    kwargs = dict(
        hold_id="hold_1",
        payer_id="payer_1",
        kind=StopReason.HARD_STOP_OPT_OUT,
        opened_at=_T0,
        reason="customer replied STOP",
        opened_by="intent_classifier",
    )
    kwargs.update(kw)
    return Hold(**kwargs)


# ------------------------------------------------- N3: how a payment was matched


def test_a_partial_payment_cannot_claim_a_payment_was_inferred_from_text():
    """§14.4: payment status is never read from a message.

    ``PartialPayment.match_method`` named ``'inferred_from_text'`` in its own
    description as the one value it must never carry, and then accepted it: the
    field was a bare ``str``. The identical value is refused one hop upstream on
    ``PaymentReceivedPayload``, so the defence existed on the event and evaporated
    on the ledger row the event writes.
    """
    with pytest.raises(ValidationError):
        PartialPayment(
            amount=Money.from_rupees(100),
            received_at=_T0,
            match_method="inferred_from_text",
        )


def test_the_ledger_row_and_its_event_share_one_match_method_vocabulary():
    """The two must not drift.

    ``PaymentReceivedPayload`` is what produces a ``PartialPayment``, so a value
    legal on one and not the other is a mapper that either drops a real payment or
    writes an unrepresentable one. The event's vocabulary is re-derived from its
    annotation rather than restated here -- a test that hard-codes the three
    strings would keep passing after someone adds a fourth to one side only.
    """
    ledger = set(get_args(PartialPayment.model_fields["match_method"].annotation))
    event = set(get_args(PaymentReceivedPayload.model_fields["match_method"].annotation))

    assert ledger, "PartialPayment.match_method is not a closed vocabulary"
    assert ledger == event, (
        f"match_method vocabularies disagree: ledger-only {sorted(ledger - event)}, "
        f"event-only {sorted(event - ledger)}"
    )


def test_every_legal_match_method_actually_constructs_a_partial_payment():
    """Walks the vocabulary rather than spot-checking ``bank_feed``: a Literal that
    accidentally excluded a value the event can emit would make a real settlement
    unrecordable, and that failure would surface in reconciliation, not here."""
    methods = get_args(PartialPayment.model_fields["match_method"].annotation)
    # Without this the loop is empty while the field is a bare ``str`` and the
    # test passes having asserted nothing -- the exact failure mode under review.
    assert len(methods) == 3, f"expected three match methods, got {methods}"
    for method in methods:
        payment = PartialPayment(
            amount=Money.from_rupees(100), received_at=_T0, match_method=method
        )
        assert payment.match_method == method


# ------------------------------------------------------- N4: consent per channel


def test_two_consent_records_for_one_channel_are_refused_in_either_order():
    """Consent must not depend on tuple order.

    ``record_for`` returns the *first* record matching a channel, and nothing
    forbade two. The pair below -- one withdrawn, one live -- made ``has_consent``
    answer False or True purely on which was listed first, on the gate that
    enforces "no contact after opt-out". Both orderings are now construction
    errors, so the ambiguity cannot be stored at all.
    """
    withdrawn = _record(withdrawn_at=_T0)
    live = _record(source="preference_centre")

    for ordering in ((withdrawn, live), (live, withdrawn)):
        with pytest.raises(ValidationError):
            ConsentProfile(payer_id="payer_1", records=ordering)


def test_a_profile_carrying_one_record_per_channel_is_still_accepted():
    """The guard against over-rejecting: uniqueness is per *channel*, not per
    record. A validator keyed on the wrong thing would refuse a payer who has
    consented on both email and SMS, which is the ordinary case."""
    profile = ConsentProfile(
        payer_id="payer_1",
        records=(_record(channel=Channel.EMAIL), _record(channel=Channel.SMS)),
    )
    assert has_consent(profile, Channel.EMAIL) is True
    assert has_consent(profile, Channel.SMS) is True
    assert has_consent(profile, Channel.WHATSAPP) is False


def test_a_withdrawn_channel_is_not_consented_however_it_is_reached():
    """``is_effective`` is the whole answer once duplicates are impossible."""
    profile = ConsentProfile(
        payer_id="payer_1", records=(_record(withdrawn_at=_T0 + timedelta(days=1)),)
    )
    assert has_consent(profile, Channel.EMAIL) is False


# --------------------------------------------------------------- N5: hold kinds


def test_a_hold_cannot_carry_a_free_text_kind():
    """``kind`` was a ``str`` whose description listed seven values in prose. A
    typo -- ``'optout'`` for ``'opt_out'`` -- constructed cleanly and produced a
    hold that no equality check downstream would ever match, silently disabling an
    immediate hard stop."""
    with pytest.raises(ValidationError):
        _hold(kind="optout")


def test_hold_kinds_are_exactly_the_seven_holds_the_plan_names():
    """§14.1's Holds row: opt-out, active dispute, hardship/vulnerability,
    bereavement, legal hold, chargeback in progress, open systemic incident
    attributable to us. Asserted by walking the frozen set, so adding a member
    without a plan row -- or dropping one that has one -- fails here."""
    assert HOLD_STOP_REASONS == frozenset(
        {
            StopReason.HARD_STOP_OPT_OUT,
            StopReason.HARD_STOP_DISPUTE,
            StopReason.HARD_STOP_HARDSHIP,
            StopReason.HARD_STOP_BEREAVEMENT,
            StopReason.HARD_STOP_LEGAL_HOLD,
            StopReason.HARD_STOP_CHARGEBACK,
            StopReason.SYSTEMIC_INCIDENT_OURS,
        }
    )
    assert len(HOLD_STOP_REASONS) == 7


def test_every_hold_stop_reason_opens_a_hold():
    for reason in HOLD_STOP_REASONS:
        assert _hold(kind=reason).kind is reason


def test_a_stop_reason_that_is_not_a_hold_cannot_open_one():
    """The complement is walked too. ``contact_cap`` and ``approval_timeout`` stop
    a *ladder*; they are not immediate hard stops on the payer, and a hold carrying
    one would suppress contact that policy still permits."""
    non_holds = set(StopReason) - HOLD_STOP_REASONS
    assert non_holds, "the complement is empty; this test would assert nothing"
    for reason in sorted(non_holds, key=lambda r: r.value):
        with pytest.raises(ValidationError):
            _hold(kind=reason)


def test_an_open_hold_reports_itself_active_until_released():
    """Unchanged behaviour, pinned because ``is_active`` is what a suppression
    check reads and the field it depends on now sits beside a new validator."""
    assert _hold().is_active is True
    assert _hold(released_at=_T0 + timedelta(hours=2)).is_active is False


# ------------------------------------------------ N7: quiet hours are optional


def test_a_payer_who_has_stated_no_quiet_hours_carries_none():
    """CONTRACTS.md §7 N7. The field used to default to a ``QuietHours()`` of
    09:00-19:00 Asia/Kolkata, which made "this payer told us their hours" and
    "this payer told us nothing" the same value. With no way to tell them apart
    the global configured window could never legitimately apply, and the two
    sources §14.1 allows had no precedence between them -- only whichever one the
    reader happened to reach for."""
    assert ConsentProfile(payer_id="payer_1").quiet_hours is None


def test_a_stated_window_is_kept_verbatim_including_its_zone():
    """The zone is the point: invariant #3 is "no contact outside quiet hours, in
    any timezone", and the payer-side record is the only one of the two sources
    that carries one."""
    profile = ConsentProfile(
        payer_id="payer_1",
        quiet_hours=QuietHours(
            start_hour_local=11, end_hour_local=16, timezone_name="Europe/Berlin"
        ),
    )
    assert profile.quiet_hours == QuietHours(
        start_hour_local=11, end_hour_local=16, timezone_name="Europe/Berlin"
    )


def test_an_inverted_stated_window_is_still_refused():
    """Unchanged behaviour, pinned because the field around it moved: an inverted
    window silently permits contact at 03:00."""
    with pytest.raises(ValidationError):
        QuietHours(start_hour_local=19, end_hour_local=9)


def test_optional_means_absent_or_a_quiet_hours_and_nothing_else():
    """Optional means "absent or a QuietHours", not "absent or anything".

    The annotation is asserted as well as the behaviour: rejecting a string was
    already true when the field was non-optional, so a test that only checked
    that would pass on the pre-amendment tree having pinned nothing about the
    change -- the vacuous-assertion failure this file's own header describes.
    """
    assert set(get_args(ConsentProfile.model_fields["quiet_hours"].annotation)) == {
        QuietHours,
        type(None),
    }
    assert ConsentProfile(payer_id="payer_1", quiet_hours=None).quiet_hours is None
    with pytest.raises(ValidationError):
        ConsentProfile(payer_id="payer_1", quiet_hours="09:00-19:00")
