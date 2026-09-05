"""§11.2 calibration anchors: **assumptions with bands, not measurements.**

§11.2's own words: "Retry-success-by-reason curves and natural recovery rates:
seeded from published industry ranges, then documented as *assumptions with a
sensitivity range*, not facts." This module is where that promise is kept, in the
place §11.2 names (``sim/anchors.py``). Every constant below carries a band and a
``source`` string saying what it was reasoned from, and none of them was fitted to
data -- because, as §11.2 states, no public labelled dataset of decline-code ->
intervention -> outcome exists at meaningful scale.

What the table encodes
----------------------
For each key (a ``DeclineClass``, or a ``RiskClass`` for a case that carries no
decline code) three numbers and one verb:

``natural``          P(the money arrives inside the recovery window with **no**
                     action from us). This is arm A0 -- §12.2's "number every naive
                     submission accidentally reports as its own".
``correct_verb``     The action family §9.2 prescribes for this failure, or ``None``
                     where §9.2's answer is "not an automated verb" (suppress
                     contact, escalate, write off).
``uplift_correct``   Additive percentage points on top of ``natural`` when the agent
                     takes ``correct_verb``.
``uplift_wrong``     Additive points when it takes some other governed verb. For the
                     four ``REQUIRES_NEW_MANDATE`` classes this is **exactly zero**,
                     because §9.2 H3 is explicit that a retry against a dead mandate
                     is 0% and still costs a failed-attempt fee. That zero is the
                     single most load-bearing number here: it is what stops a retry
                     engine from scoring like a recovery engine.

Why additive and not a hazard model
-----------------------------------
An additive uplift clamped at a ceiling is legible -- "+22pp on NSF, assumed" is a
claim a judge can push back on in one sentence, and §12.5.3's live challenge invites
exactly that. A multiplicative or hazard formulation would hide the same guess behind
more arithmetic and imply a timing model that arm A2 was cut before building. The
cost is that the uplift does not interact with amount, segment, attempt number or
the payer's calendar, so the simulator cannot express "the same action works better
on the 3rd of the month" -- which is precisely the effect §12.2's arm A2 exists to
measure. A2 is cut (§18.4), so nothing here needs it, and nothing here may claim it.

Arm A1's uplift is deliberately **one number for every failure reason**. That is what
makes it §12.2's naive baseline: a static drip that does not read the decline code.
It is modelled as four decaying touches rather than one, because §12.2 specifies a
"static 4-email drip" and crediting A1 with a single touch would inflate every
increment measured against it.

Nothing in this module does I/O, reads a clock, or touches a database; it is tables
and pure functions over ``Decimal``. Float arithmetic never appears: probabilities
are fixed-scale ``Decimal`` at ``units.PROBABILITY_SCALE`` so a drawn outcome is
reproducible byte-for-byte and can go straight into an audit digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from reclaim.contracts.actions import ActionType
from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.enums import RiskClass
from reclaim.contracts.units import probability

__all__ = [
    "ANCHORS_VERSION",
    "ANCHOR_HONESTY_NOTE",
    "DECLINE_ANCHORS",
    "GENERIC_TOTAL_UPLIFT",
    "GENERIC_TOUCH_COUNT",
    "GENERIC_TOUCH_DECAY",
    "GENERIC_UPLIFT_FIRST_TOUCH",
    "HEDGED_UPLIFT_SHARE",
    "HEDGED_UPLIFT_SHARE_BAND",
    "RECOVERY_PROBABILITY_CEILING",
    "RISK_CLASS_ANCHORS",
    "RecoveryAnchor",
    "all_anchors",
    "generic_probability",
    "natural_probability",
    "probability",
    "targeted_probability",
]

#: Bumped whenever any number below moves. Recorded on every simulated outcome's
#: audit row, so a scoreboard can be traced to the exact assumption set that
#: produced it -- §12.5.1's pre-registration is worthless if the environment can
#: drift silently between the registered run and the reported one.
ANCHORS_VERSION = "0.1.0"

#: §11.2's "honest statement we will put on a slide", as a constant rather than a
#: comment so the demo can print it next to the scoreboard and a test can assert it
#: still exists.
ANCHOR_HONESTY_NOTE = (
    "Every recovery probability in reclaim.sim.anchors is an ASSUMPTION reasoned "
    "from published industry ranges, not real observed data: no public labelled "
    "dataset of decline-code -> intervention -> outcome exists at meaningful "
    "scale (HACKATHON_PLAN.md 11.2). The absolute rates below are therefore "
    "environment-dependent and are not a claim about any real portfolio. The only "
    "claim they support is the COMPARISON BETWEEN ARMS inside this one simulated "
    "environment, which is randomised, pre-registered and reproducible from seed."
)

#: No intervention is modelled as certain. A ceiling also keeps
#: ``natural + uplift`` inside [0, 1] without the caller checking.
RECOVERY_PROBABILITY_CEILING: Decimal = probability("0.95")

#: What share of a correctly targeted contact's uplift a **hedged** contact earns
#: -- the one non-committal message ``flow.hedged_route`` sends when the diagnosis
#: is contested and the candidate causes want opposite things.
#:
#: **This is an assumption, not a measurement.** The reasoning is that the
#: hedge reaches *the same audience* as the targeted message -- it is delivered on
#: the same channel, to the same payer, carrying the same working link -- but with
#: a weaker call to action, because it reports a fact and offers two doors instead
#: of asking for one thing. Same reach, worse conversion. Half is the midpoint of
#: a band nobody has data for.
#:
#: Swept over its whole band, the headline moves ₹0.16 L on the n=200 seed and
#: ₹1.43 L on n=2000 under the eval allocation -- about 7% of a -₹20 L estimate.
#: So the constant is **not** load-bearing today, and that was measured rather
#: than assumed. It is a statement about how few cases reach the discounted lane
#: (40 of 727 A4 cases at n=2000), not about how well the number is known: if the
#: consent stand-in stops denying a third of all contacts, or a scheduler lands
#: and quiet-hours denials become deferrals, the hedged population grows and this
#: constant starts to matter. Re-sweep before quoting a headline, per §12.5.3.
HEDGED_UPLIFT_SHARE: Decimal = Decimal("0.5")

#: The band ``HEDGED_UPLIFT_SHARE`` was invented within. The low end says a hedge
#: is worth a quarter of the right message (most of the audience needed the
#: specific instruction); the high end says three quarters (the link did the work
#: and the copy barely mattered). Nothing here rules out either.
HEDGED_UPLIFT_SHARE_BAND: tuple[Decimal, Decimal] = (Decimal("0.25"), Decimal("0.75"))

# --- arm A1: the static drip -------------------------------------------------
#
# ASSUMPTION (band 0.015-0.050 for the first touch): a generic dunning email that
# does not diagnose anything converts a low single-digit share of failures. The
# decay says each successive touch of an identical drip is worth less than the one
# before -- the payers a generic message can move are moved by the first one.

GENERIC_UPLIFT_FIRST_TOUCH: Decimal = probability("0.030")
GENERIC_TOUCH_DECAY: Decimal = Decimal("0.6")
GENERIC_TOUCH_COUNT: int = 4

#: The whole drip, collapsed to one number. §12.2's A1 is a *fixed* schedule, so
#: there is nothing per-case to vary and no reason to walk the touches one at a
#: time. Derived rather than written down so changing the decay cannot leave a
#: stale total behind.
GENERIC_TOTAL_UPLIFT: Decimal = probability(
    sum(
        (GENERIC_UPLIFT_FIRST_TOUCH * GENERIC_TOUCH_DECAY**i
         for i in range(GENERIC_TOUCH_COUNT)),
        Decimal(0),
    )
)


@dataclass(frozen=True)
class RecoveryAnchor:
    """One row of the calibration table. See the module docstring for the fields."""

    natural: Decimal
    natural_band: tuple[Decimal, Decimal]
    correct_verb: ActionType | None
    uplift_correct: Decimal
    uplift_wrong: Decimal
    source: str


def _a(
    natural: str,
    band: tuple[str, str],
    *,
    verb: ActionType | None,
    correct: str,
    wrong: str = "0",
    source: str,
) -> RecoveryAnchor:
    return RecoveryAnchor(
        natural=probability(natural),
        natural_band=(probability(band[0]), probability(band[1])),
        correct_verb=verb,
        uplift_correct=probability(correct),
        uplift_wrong=probability(wrong),
        source=source,
    )


_DEBIT = ActionType.SCHEDULE_DEBIT
_MSG = ActionType.SEND_MESSAGE


#: Keyed by the *observed* decline class, never by the inferred root cause: the
#: simulator must not be able to see the agent's diagnosis, or the environment
#: would be rewarding the agent for agreeing with it.
DECLINE_ANCHORS: Mapping[DeclineClass, RecoveryAnchor] = {
    DeclineClass.INSUFFICIENT_FUNDS: _a(
        "0.35", ("0.25", "0.45"),
        verb=_DEBIT, correct="0.22", wrong="0.04",
        source="NSF self-cures at the next liquidity peak more often than any other "
        "class; re-timing the debit is the whole intervention (9.2 H1) and a "
        "contact adds little. Band is wide because it is entirely calendar-driven.",
    ),
    DeclineClass.ISSUER_TRANSIENT_DECLINE: _a(
        "0.42", ("0.30", "0.55"),
        verb=_DEBIT, correct="0.18", wrong="0.03",
        source="Transient issuer declines clear on their own most often of all; the "
        "agent's value is re-presenting sooner rather than at the next cycle.",
    ),
    DeclineClass.ISSUER_UNAVAILABLE: _a(
        "0.30", ("0.20", "0.42"),
        verb=None, correct="0", wrong="0",
        source="An outage resolves without us. 9.2 H6 says suppress contact and wait "
        "for the incident to close, so no governed verb here earns credit.",
    ),
    DeclineClass.AUTHENTICATION_REQUIRED: _a(
        "0.12", ("0.06", "0.20"),
        verb=_MSG, correct="0.20", wrong="0",
        source="A hard decline per Stripe's India docs: nothing happens until a human "
        "completes the step-up, which is what the AFA-completion contact asks for.",
    ),
    DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS: _a(
        "0.18", ("0.10", "0.28"),
        verb=_MSG, correct="0.15", wrong="0",
        source="The ambiguous class the product exists for: three root causes wanting "
        "opposite actions, so even the right verb helps only the AFA subset. Its "
        "uplift is deliberately the most pessimistic of the contactable classes.",
    ),
    DeclineClass.MANDATE_INVALID: _a(
        "0.06", ("0.02", "0.12"),
        verb=_MSG, correct="0.12", wrong="0",
        source="9.2 H3: recovery is a re-registration journey. A retry is 0% and "
        "still costs a failed-attempt fee, hence uplift_wrong = 0 exactly.",
    ),
    DeclineClass.MANDATE_CANCELLED: _a(
        "0.05", ("0.02", "0.10"),
        verb=_MSG, correct="0.11", wrong="0",
        source="Cancelled mandates are usually a deliberate act, so natural recovery "
        "is near the floor and re-registration is opt-in. Stripe: mandates cannot "
        "be cancelled or updated via API, so there is no automated fix at all.",
    ),
    DeclineClass.MANDATE_PAUSED: _a(
        "0.08", ("0.03", "0.15"),
        verb=_MSG, correct="0.13", wrong="0",
        source="Paused reads as reversible more often than cancelled, but it is also "
        "one of 9.2 H5's churn signals; the uplift assumes a share of these payers "
        "are leaving and no message will hold them.",
    ),
    DeclineClass.MANDATE_EXPIRED: _a(
        "0.07", ("0.03", "0.14"),
        verb=_MSG, correct="0.12", wrong="0",
        source="Expiry is nobody's decision, so a re-registration prompt lands better "
        "than on a cancellation -- but it is still a journey, never a retry.",
    ),
    DeclineClass.MANDATE_CAP_EXCEEDED: _a(
        "0.06", ("0.02", "0.12"),
        verb=_MSG, correct="0.10", wrong="0",
        source="A UPI Autopay debit over the Rs 15,000 per-transaction cap cannot "
        "succeed on that rail at any time; the fix is a new mandate or a different "
        "rail, and the cap is ours to have got wrong (is_our_side).",
    ),
    DeclineClass.PRE_DEBIT_NOTIFICATION_UNDELIVERED: _a(
        "0.15", ("0.08", "0.25"),
        verb=_MSG, correct="0.18", wrong="0.02",
        source="Our failure, and the cheapest to fix: re-notify and the 24h RBI "
        "window can be satisfied properly. The small wrong-verb uplift reflects that "
        "a re-presented debit occasionally lands after the notice finally arrives.",
    ),
    DeclineClass.CARD_EXPIRED: _a(
        "0.12", ("0.06", "0.20"),
        verb=_MSG, correct="0.16", wrong="0",
        source="Stripe: retries 'only execute if you obtain a new payment method', so "
        "the credential-update journey is the only path. Natural recovery is whoever "
        "updates their card for some other reason inside the window.",
    ),
    DeclineClass.CARD_REPLACED_OR_TOKEN_INVALID: _a(
        "0.10", ("0.05", "0.18"),
        verb=_MSG, correct="0.15", wrong="0",
        source="Same mechanism as expiry, slightly lower natural recovery because the "
        "payer has no expiry date to prompt them.",
    ),
    DeclineClass.CARD_LOST_OR_STOLEN: _a(
        "0.04", ("0.01", "0.09"),
        verb=_MSG, correct="0.09", wrong="0",
        source="Terminal on the instrument and often on the relationship; a "
        "credential-update prompt is legitimate but lands badly, so the uplift is the "
        "smallest non-zero one in the table.",
    ),
    DeclineClass.ACCOUNT_CLOSED_OR_INVALID: _a(
        "0.03", ("0.01", "0.08"),
        verb=None, correct="0", wrong="0",
        source="No instrument, no mandate, frequently no customer. 10.2 has no verb "
        "for this and inventing an uplift would reward the agent for acting anyway.",
    ),
    DeclineClass.RISK_BLOCKED_BY_ISSUER: _a(
        "0.08", ("0.03", "0.16"),
        verb=None, correct="0", wrong="0",
        source="An issuer risk block is opaque and not ours to lift; 9.2 routes it to "
        "a human (H6/H7), which is not a governed automated verb.",
    ),
    DeclineClass.RISK_BLOCKED_BY_PSP: _a(
        "0.25", ("0.15", "0.38"),
        verb=_DEBIT, correct="0.15", wrong="0",
        source="PSP-side blocks are often rule-and-window shaped, so re-presenting "
        "after the incident clears is genuinely effective -- once the incident clears.",
    ),
    DeclineClass.RISK_BLOCKED_BY_MERCHANT_RULE: _a(
        "0.20", ("0.10", "0.32"),
        verb=_DEBIT, correct="0.17", wrong="0",
        source="Our own rule blocked our own money. Highest fixability in the risk "
        "family, and the residual claim is 9.2 H6's proposed route change (a T2 verb "
        "this simulator does not model).",
    ),
    DeclineClass.PROCESSING_ERROR: _a(
        "0.40", ("0.28", "0.52"),
        verb=_DEBIT, correct="0.25", wrong="0",
        source="A technical failure that never reached a decision; a clean retry is "
        "the highest-yield action in the whole table, which is exactly why a retry "
        "engine looks good on a portfolio dominated by these.",
    ),
    DeclineClass.ROUTING_OR_CONFIG_ERROR: _a(
        "0.35", ("0.22", "0.48"),
        verb=_DEBIT, correct="0.23", wrong="0",
        source="Same shape as a processing error, one notch lower because the "
        "misconfiguration usually persists until someone fixes it.",
    ),
    DeclineClass.RAIL_NOT_SUPPORTED: _a(
        "0.05", ("0.01", "0.12"),
        verb=None, correct="0", wrong="0",
        source="Structurally impossible on this rail; the fix is a route change, "
        "which is a T2 human decision and not modelled here.",
    ),
    DeclineClass.DUPLICATE_ATTEMPT_BLOCKED: _a(
        "0.50", ("0.35", "0.65"),
        verb=None, correct="0", wrong="0",
        source="The highest natural recovery in the table and not a success of ours: "
        "a duplicate was blocked because the original attempt most likely went "
        "through. Any uplift credited here would be double-counting.",
    ),
    DeclineClass.NETWORK_RETRY_LIMIT_EXCEEDED: _a(
        "0.15", ("0.07", "0.26"),
        verb=None, correct="0", wrong="0",
        source="The rail forbids another attempt regardless of what we believe; "
        "14.1's compliance hold means no verb is available at all.",
    ),
    DeclineClass.UNKNOWN_UNMAPPED: _a(
        "0.10", ("0.03", "0.22"),
        verb=None, correct="0", wrong="0",
        source="Fail closed (16, Data): an unmapped code gets no automated debit and "
        "no assumed uplift. Crediting the unknown bucket would let a normaliser "
        "regression show up as a recovery improvement.",
    ),
}


#: Used only when a case carries **no** decline class -- which today means a D3
#: overdue receivable (JC-42: no other detector observes a PSP code).
RISK_CLASS_ANCHORS: Mapping[RiskClass, RecoveryAnchor] = {
    RiskClass.OVERDUE_RECEIVABLE: _a(
        "0.30", ("0.20", "0.42"),
        verb=_MSG, correct="0.14", wrong="0",
        source="11.1's B2B reply mix: 45% never reply and 12% pay after contact, on "
        "top of a large share of invoices that get paid late without any chasing. "
        "The uplift is that 12% plus a little of the 22% conditional-promise band.",
    ),
    RiskClass.FAILED_RECURRING_DEBIT: _a(
        "0.20", ("0.10", "0.32"),
        verb=None, correct="0", wrong="0",
        source="Only reachable for a D1 case whose code was never normalised, which "
        "is a normaliser bug, not a payer state. Deliberately gets no uplift so the "
        "bug cannot look like recovery.",
    ),
    RiskClass.PREDICTED_TO_FAIL_DEBIT: _a(
        "0.55", ("0.40", "0.70"),
        verb=None, correct="0", wrong="0",
        source="D2 is not built (18.3). High natural recovery because a *prediction* "
        "of failure is not a failure; no uplift because there is no D2 intervention "
        "to credit.",
    ),
    RiskClass.CHECKOUT_ABANDONMENT: _a(
        "0.25", ("0.15", "0.38"),
        verb=None, correct="0", wrong="0",
        source="D4 is not built. Placeholder so the table is total; a value here would "
        "be a number for a detector that does not exist.",
    ),
    RiskClass.SYSTEMIC_AUTH_DEGRADATION: _a(
        "0.45", ("0.30", "0.60"),
        verb=None, correct="0", wrong="0",
        source="D5's cohort cases resolve when the incident does. 9.2 H6 suppresses "
        "contact, so there is no verb to credit even once D5 lands.",
    ),
    RiskClass.SILENT_LEAKAGE: _a(
        "0.10", ("0.03", "0.22"),
        verb=None, correct="0", wrong="0",
        source="D6 is a stretch item and its recovery path is a human reconciliation, "
        "not an automated verb.",
    ),
}


def all_anchors() -> dict[DeclineClass | RiskClass, RecoveryAnchor]:
    """Both tables in one mapping, for a test that has to walk every row.

    The two key types are disjoint enums, so the merge cannot collide.
    """
    return {**DECLINE_ANCHORS, **RISK_CLASS_ANCHORS}


def _anchor_for(
    decline_class: DeclineClass | None, risk_class: RiskClass | None
) -> RecoveryAnchor:
    """The anchor for a case. The decline class wins when there is one.

    Both ``None`` is a caller bug, not a case shape: every ``RiskCase`` carries a
    risk class, so reaching here without one means something built a case-like
    object by hand. Raising beats returning a default nobody chose.
    """
    if decline_class is not None:
        return DECLINE_ANCHORS[decline_class]
    if risk_class is not None:
        return RISK_CLASS_ANCHORS[risk_class]
    raise ValueError(
        "an anchor needs a decline class or a risk class; both were None"
    )


def _clamp(value: Decimal) -> Decimal:
    return probability(min(value, RECOVERY_PROBABILITY_CEILING))


def natural_probability(
    decline_class: DeclineClass | None, risk_class: RiskClass | None
) -> Decimal:
    """Arm A0: P(recovered in window | nothing was done). §12.2's natural floor."""
    return _clamp(_anchor_for(decline_class, risk_class).natural)


def generic_probability(
    decline_class: DeclineClass | None, risk_class: RiskClass | None
) -> Decimal:
    """Arm A1: natural recovery plus one undifferentiated static drip.

    The uplift does not read ``decline_class`` -- it is passed only to find the
    natural baseline it sits on top of. That asymmetry *is* arm A1.
    """
    return _clamp(
        _anchor_for(decline_class, risk_class).natural + GENERIC_TOTAL_UPLIFT
    )


def targeted_probability(
    decline_class: DeclineClass | None,
    risk_class: RiskClass | None,
    verb: ActionType | None,
    *,
    hedged: bool = False,
) -> Decimal:
    """Arm A4: natural recovery plus an uplift that depends on the verb chosen.

    ``verb=None`` means no action was taken, which is the natural baseline. Choosing
    the verb §9.2 prescribes earns ``uplift_correct``; any other governed verb earns
    ``uplift_wrong``, which for a dead mandate is zero. This is the only place the
    simulator rewards *diagnosis quality* rather than activity, and it is the reason
    an A4 number here is not automatically above A1's.

    ``hedged=True`` is the third case: the right *verb*, sent without having
    resolved the cause -- ``flow``'s contested-dispatch fallback. It earns
    ``HEDGED_UPLIFT_SHARE`` of the targeted uplift, which keeps the fallback's
    coverage from reading as diagnostic skill. See that constant for how much of
    the resulting headline rests on a number nobody has measured.
    """
    anchor = _anchor_for(decline_class, risk_class)
    if verb is None:
        return _clamp(anchor.natural)
    if anchor.correct_verb is not None and verb is anchor.correct_verb:
        uplift = anchor.uplift_correct
        if hedged:
            # Only the *correct* verb is discounted. A hedge is not a licence for
            # a mistargeted action to earn something it otherwise would not --
            # ``uplift_wrong`` is zero for the mandate classes on purpose (§9.2
            # H3), and multiplying zero by a share must not become a way around
            # that.
            uplift = probability(uplift * HEDGED_UPLIFT_SHARE)
    else:
        uplift = anchor.uplift_wrong
    return _clamp(anchor.natural + uplift)


# --- import-time guards ------------------------------------------------------
#
# The tables above are enum-keyed mappings, and CLAUDE.md's rule for those is that a
# table is frozen only when something walks every row. The tests do; these guards
# make a missing row a failed *import* as well, so a partially-updated table cannot
# be used by a script that never runs the suite.

_missing_declines = set(DeclineClass) - set(DECLINE_ANCHORS)
if _missing_declines:  # pragma: no cover - import-time guard
    raise RuntimeError(
        f"DECLINE_ANCHORS is missing entries for: "
        f"{sorted(c.value for c in _missing_declines)}"
    )

_missing_risks = set(RiskClass) - set(RISK_CLASS_ANCHORS)
if _missing_risks:  # pragma: no cover - import-time guard
    raise RuntimeError(
        f"RISK_CLASS_ANCHORS is missing entries for: "
        f"{sorted(c.value for c in _missing_risks)}"
    )

for _key, _anchor in all_anchors().items():
    _low, _high = _anchor.natural_band
    if not _low <= _anchor.natural <= _high:  # pragma: no cover - import-time guard
        raise RuntimeError(
            f"anchor {_key} has a band {_low}-{_high} that excludes its own point "
            f"estimate {_anchor.natural}"
        )
    if _anchor.natural + _anchor.uplift_correct > Decimal(1):  # pragma: no cover
        raise RuntimeError(f"anchor {_key} can exceed probability 1")

del _key, _anchor, _low, _high, _missing_declines, _missing_risks
