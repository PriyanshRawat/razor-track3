"""The **simulated PSP response layer**: did the money arrive? (§12.2 arms A0/A1/A4)

Nothing in this module talks to a payment rail. It reads a case out of the ledger
*after* ``flow`` has finished with it, draws an outcome from ``anchors``, walks §9.1's
state machine to the state that outcome implies, and writes one audit row that says
``simulated_psp_response`` in those words. Every number it produces is a property of
the simulated environment, not of any real portfolio -- see
``anchors.ANCHOR_HONESTY_NOTE``, which is the sentence that belongs on the slide.

The four lanes, and why they are not symmetric
----------------------------------------------
=============  =====  ===============  =================================================
Lane           Arm    Entry state      What is simulated
=============  =====  ===============  =================================================
NATURAL_ONLY   A0     ``detected``     Natural recovery only: no diagnosis, no policy
                                       evaluation, no action, no contact. §12.2's floor.
GENERIC        A1     ``detected``     One undifferentiated static 4-touch drip that
                                       never reads the decline code, with a modest flat
                                       uplift. §12.2's "realistic industry baseline".
TARGETED       A4     ``scheduled``    The action the agent actually enqueued, with an
                                       uplift that depends on whether that verb is the
                                       one §9.2 prescribes for the observed class.
NATURAL_FLOOR  any    anything else    An in-scope case the agent did **not** act on:
               in                      escalated below the confidence floor, stopped by
               scope                   policy, or parked for a T2 approval. Natural
                                       recovery only -- no uplift, no verb, no contact.
=============  =====  ===============  =================================================

**Why the floor exists, and what it fixes.** Every case is randomised into an arm at
creation and §12.1's unit is that case, so an intent-to-treat estimate keeps it in the
denominator whatever the agent later decided. Before this lane, such a case resolved to
*nothing* and the estimator scored it as **zero recovered** -- which asserts that a case
recovers no money at all because the agent declined to act on it. That is false: natural
recovery does not switch off. Zero was the wrong floor, and on the seeded batch it
understated A4 by about two thirds of its recovered rupees and flipped the sign of the
headline. The right floor is the same natural probability arm A0 gets, and no more --
crediting an escalated case with A4's *targeted* uplift would pay the agent for work a
human has not done yet.

Only A2/A3/A5 come back ``NOT_SIMULATED`` now (cut at T-12h per §18.4, plus A3 by this
pass's own scope decision), along with any case a previous pass already resolved.
Giving an unscored arm a floor would manufacture a number for an arm nobody built.

**A parked case is still never auto-approved.** It gets the floor, because natural
recovery ran while the human was deciding -- but ``awaiting_approval -> scheduled`` is
the *approve* edge and the simulator never takes it. A parked case that self-heals ends
``STOPPED(already_paid)``; a parked case that does not is left exactly where the human
left it.

Four deliberate compromises, each of which changes how a number here reads
-------------------------------------------------------------------------
1. **A0 recovery is recorded as ``STOPPED(already_paid)``, not ``RECOVERED``.**
   §9.1 has no edge from ``detected`` to ``recovered``: every path into ``recovered``
   runs through ``executing`` or a reconciliation, and both mean somebody acted.
   Rather than add an edge to a frozen table or fake a plan on the control arm (which
   ``RiskCase`` refuses outright), a self-healed control case is stopped with the
   reason that is literally true -- the money arrived without us. The consequence is
   sharp and must not be forgotten: **the recovered amount lives on
   ``SimulatedOutcome``, never on ``state is RECOVERED``.** Any metric that counts
   recovered cases by state scores A0 at zero and inflates every arm's lift.
2. **A1's drip is never enqueued and never policy-checked.** The outbox is
   exactly-once *execution* of an action the policy engine approved; A1's baseline
   was proposed by nobody and evaluated by nothing, so putting it there would make a
   fabricated action indistinguishable from a governed one. The cost: A1's simulated
   contacts are not consent-gated or quiet-hours-gated, so its recovery is that of a
   baseline that *ignores* §14.1. A compliant baseline would score slightly lower,
   which makes A4's lift over A1 a mild under-statement -- and A1's (uncounted)
   violations a real, separate story this module does not tell.
3. **A4's uplift is calibrated for deterministic routing, not for an LLM.** No LLM
   path exists in this repository yet: ``flow.route`` is A3's mechanism, and A3 and
   A4 run identically through it. So the TARGETED lane measures §12.2's
   "value of intervention choice", reported under A4's name because A4 is the arm
   the seeded data assigns cases to. **The A4-minus-A3 decomposition -- the value of
   the LLM, which §12.2 says must be measured and not asserted -- is not computed
   here and cannot be, with A3 out of scope.**
4. **The natural floor is applied uniformly, including to policy-denied cases.** A
   case denied because of an open dispute or a withdrawn consent probably does *not*
   self-heal at the population rate -- an open dispute is itself evidence the money is
   contested. There is no model for that, so the floor uses the same natural anchor as
   everything else, which mildly over-credits the denied slice. Zero would be worse by
   far, and the alternative (a per-stop-reason floor) would be a fourth table of
   numbers with no more evidence behind it than this one.

Determinism
-----------
The outcome is a pure function of ``(SIM_SALT, case_id, arm)`` through
``hashlib.sha256`` -- never the builtin ``hash()``, which is ``PYTHONHASHSEED``-
randomised and would pass every in-process test while silently re-rolling the whole
book between the pre-registered run and the judge's reproduction (invariant #5,
``experiment.py`` JC-37). The arm is in the payload on purpose: the same case gets a
different draw in a different arm, so a lucky case cannot carry its luck across the
comparison. ``SIM_SALT`` is subject to the same rule as the experiment salt -- change
it after the eval run and the reported scoreboard is void (§12.5.1).

Time is derived, never read: an outcome is observed at the case's
``recovery_window_ends_at``, which is §12.1's fixed horizon. There is no clock call
anywhere in this module, so a replay decides identically whenever it runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from reclaim.contracts.actions import ActionEnvelope, ActionType
from reclaim.contracts.canonical import digest
from reclaim.contracts.case import RiskCase
from reclaim.contracts.decline_taxonomy import (
    DECLINE_CLASS_META,
    DeclineClass,
    Retryability,
)
from reclaim.contracts.enums import (
    Arm,
    ActorType,
    CaseState,
    MessageIntent,
    RiskClass,
    StopReason,
)
from reclaim.contracts.metrics import recovery_rate
from reclaim.contracts.money import Money, money_sum
from reclaim.contracts.units import probability, ratio
from reclaim.sim.anchors import (
    ANCHORS_VERSION,
    GENERIC_TOUCH_COUNT,
    generic_probability,
    natural_probability,
    targeted_probability,
)
from reclaim.spine import audit_store, case_machine, ledger
from reclaim.spine.tables import audit_log, outbox as outbox_table

__all__ = [
    "ArmTally",
    "BatchResolution",
    "HEDGED_CONTACT_INTENT",
    "IN_SCOPE_ARMS",
    "SIMULATED_RESPONSE_EVENT",
    "SIM_SALT",
    "SimLane",
    "SimulatedOutcome",
    "draw_for",
    "hedged_contact_enqueued",
    "resolve_batch",
    "resolve_case",
    "tally_by_arm",
]

#: Independent of the *experiment* salt. Arm assignment must not move when the
#: environment is re-rolled, and vice versa; sharing one salt would couple
#: "which arm is this case in" to "did it get lucky".
SIM_SALT = "reclaim-sim-psp-v1"

#: Deliberately **not** a member of ``audit.AUDIT_EVENT_TYPES``. That frozen set is
#: the vocabulary of real log points (JC-31: open, unenforced). Keeping this event
#: outside it means a reviewer grepping the set can tell at a glance which rows
#: never touched a rail -- and it is why this module does not reuse
#: ``action_executed``.
SIMULATED_RESPONSE_EVENT = "simulated_psp_response"

#: §18.4's T-12h cut keeps A0, A1 and A4. A3 is dropped by this pass as well, which
#: costs the A4-A3 decomposition -- see the module docstring, compromise 3.
#: The message intent that marks a contact as ``flow``'s contested-dispatch
#: hedge. Named from the frozen ``MessageIntent`` enum rather than imported from
#: ``flow.HEDGED_DECLINE_INTENT``, because §12.5.4 item 4 forbids the simulator
#: from importing agent code -- a sim that can reach ``flow`` is somewhere a
#: detector could learn the answer. The duplication is deliberate and is pinned:
#: ``tests/test_sim_outcomes.py`` asserts the two constants are the same member,
#: so changing one without the other fails the build rather than silently
#: scoring every hedge at full price.
HEDGED_CONTACT_INTENT: MessageIntent = MessageIntent.PAYMENT_FAILED_INFORM

IN_SCOPE_ARMS: frozenset[Arm] = frozenset({Arm.A0, Arm.A1, Arm.A4})

#: A failed debit permits another attempt only on these two. Derived by subtraction
#: so a *new* ``Retryability`` member defaults to "no further debit" -- the
#: fail-closed direction (§16 Data).
_FURTHER_DEBIT_OK: frozenset[Retryability] = frozenset(
    {Retryability.RETRY_SOFT, Retryability.RETRY_AFTER_INCIDENT}
)


class SimLane(StrEnum):
    """Which mechanism produced this case's outcome. See the docstring's table."""

    NATURAL_ONLY = "natural_only"
    GENERIC_BASELINE = "generic_baseline"
    TARGETED_AGENT = "targeted_agent"
    NATURAL_FLOOR = "natural_floor"
    NOT_SIMULATED = "not_simulated"


@dataclass(frozen=True)
class SimulatedOutcome:
    """One case's simulated result. ``recovered_amount`` is the authoritative
    recovered figure -- **not** ``final_state is RECOVERED``, which A0 can never
    reach (compromise 1)."""

    case_id: str
    arm: Arm
    lane: SimLane
    decline_class: DeclineClass | None
    risk_class: RiskClass
    amount_at_risk: Money
    entry_state: CaseState
    final_state: CaseState
    action_type: ActionType | None
    recovered: bool
    recovered_amount: Money
    probability: Decimal | None
    draw: Decimal | None
    simulated_contacts: int
    reason: str
    #: True when the enqueued contact was ``flow``'s contested-dispatch
    #: hedge, so the draw used a discounted uplift. Carried on the outcome
    #: (not only in the audit row) because a scoreboard that cannot separate
    #: hedged recoveries from targeted ones cannot report either honestly.
    hedged: bool = False

    @property
    def was_simulated(self) -> bool:
        return self.lane is not SimLane.NOT_SIMULATED


@dataclass(frozen=True)
class ArmTally:
    """One arm's totals over a resolved batch.

    Not an ``ArmOutcome`` (``contracts.metrics``): that model is per *stratum* and
    carries a ``CostBreakdown`` this layer does not model. Cost to collect is
    therefore unknown here, so §12.1's headline -- **net** incremental recovery --
    cannot be computed from a tally, and this class deliberately offers no field
    that looks like it. What it offers is the gross figure and the case counts.
    """

    arm: Arm
    case_count: int
    simulated_case_count: int
    recovered_case_count: int
    total_at_risk: Money
    simulated_at_risk: Money
    gross_recovered: Money
    simulated_contacts: int

    @property
    def recovery_rate(self) -> Decimal | None:
        """Recovered cases / cases, or ``None`` for an empty arm (JC-33).

        Over *all* the arm's cases, including the ones that were never simulated:
        an unresolved case is a case that did not recover, and quietly dropping it
        from the denominator would turn "we could not resolve this" into a higher
        recovery rate.
        """
        return recovery_rate(
            recovered_obligations=self.recovered_case_count,
            at_risk_obligations=self.case_count,
        )

    @property
    def gross_recovered_per_rupee_at_risk(self) -> Decimal | None:
        """Over **all** the arm's at-risk rupees, resolved or not.

        Gross, not net. §12.1's estimator differences the *net* rate; naming this
        one honestly is what stops it being quoted as the headline.

        Read this number **only alongside** ``simulated_at_risk``. A0 and A1 get a
        draw for every case, but A4 resolves only the cases the policy engine
        auto-allowed -- escalations, denials and approval-parked cases have no
        simulated outcome and land in this denominator as zero recovered. Across
        arms with different coverage it is therefore not a comparable rate, and on
        the seeded batch it reads as though the agent lost money it never got to
        act on.
        """
        if self.total_at_risk.is_zero:
            return None
        return ratio(self.gross_recovered.ratio_to(self.total_at_risk))

    @property
    def gross_recovered_per_simulated_rupee(self) -> Decimal | None:
        """Over only the at-risk rupees this arm actually resolved.

        The comparable rate: every arm's numerator and denominator then cover the
        same set of cases. ``None`` when the arm resolved nothing (JC-33: an arm
        nobody built has no rate, and a zero would be a claim about it).

        The catch is sample size, not definition: A4's resolved subset is small and
        is **not** a random subsample of A4 -- it is exactly the cases the router
        had a verb for and policy allowed, so it is biased towards the easy ones.
        Neither rate is §12.1's estimator; that one is stratum-weighted, net of
        cost, and bootstrapped, and none of those three exists yet.
        """
        if self.simulated_at_risk.is_zero:
            return None
        return ratio(self.gross_recovered.ratio_to(self.simulated_at_risk))


@dataclass(frozen=True)
class BatchResolution:
    """Every outcome in a batch, plus the per-arm totals."""

    outcomes: tuple[SimulatedOutcome, ...]
    by_arm: Mapping[Arm, ArmTally]

    @property
    def simulated_count(self) -> int:
        return sum(1 for o in self.outcomes if o.was_simulated)


# ---------------------------------------------------------------------------
# The draw
# ---------------------------------------------------------------------------


def draw_for(case_id: str, arm: Arm, *, salt: str = SIM_SALT) -> Decimal:
    """A uniform draw in [0, 1) for this case in this arm.

    Recipe, stated so a test can re-derive it without asking this function:
    ``sha256(f"{salt}|{case_id}|{arm.value}")``, first 8 bytes big-endian, divided by
    2**64, quantised to ``units.PROBABILITY_SCALE``. All ``Decimal``: no float ever
    enters, so the draw is exact, reproducible, and safe to put in an audit digest.
    """
    payload = f"{salt}|{case_id}|{arm.value}".encode("utf-8")
    raw = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return probability(Decimal(raw) / Decimal(2**64))


# ---------------------------------------------------------------------------
# Lane selection
# ---------------------------------------------------------------------------


def _already_resolved(conn: Connection, case_id: str) -> bool:
    """Whether this case already carries a ``simulated_psp_response`` row.

    This -- not the case's state -- is what makes ``resolve_batch`` idempotent.
    State was load-bearing for that before the natural floor existed, and it was
    already leaking: an A0 case that did *not* recover stays in ``detected``, so a
    second pass re-drew it and wrote a second row. Now that every in-scope case
    resolves, a state-only check would re-draw every non-recovered case in the
    book. The audit row is the authoritative record that a case has been resolved,
    so the audit row is what gets asked.
    """
    found = conn.execute(
        sa.select(audit_log.c.sequence)
        .where(
            audit_log.c.case_id == case_id,
            audit_log.c.event_type == SIMULATED_RESPONSE_EVENT,
        )
        .limit(1)
    ).first()
    return found is not None


def _lane_for(conn: Connection, case: RiskCase) -> tuple[SimLane, str]:
    """Which lane a case belongs to, or why it gets none."""
    if _already_resolved(conn, case.case_id):
        return (
            SimLane.NOT_SIMULATED,
            "the case already carries a simulated_psp_response row: it was "
            "resolved by an earlier pass and a second draw would double-count it",
        )

    if case.arm not in IN_SCOPE_ARMS:
        return (
            SimLane.NOT_SIMULATED,
            f"arm {case.arm.value} is outside this pass's scope: the T-12h cut "
            f"(§18.4) keeps A0/A1/A4, and A3 was dropped with them",
        )

    if case.arm is Arm.A0:
        return (
            (SimLane.NATURAL_ONLY, "")
            if case.state is CaseState.DETECTED
            else (
                SimLane.NATURAL_FLOOR,
                f"control-arm case is in {case.state.value} rather than detected; "
                "something moved it, so only the natural floor applies",
            )
        )

    if case.arm is Arm.A1:
        return (
            (SimLane.GENERIC_BASELINE, "")
            if case.state is CaseState.DETECTED
            else (
                SimLane.NATURAL_FLOOR,
                f"baseline-arm case is in {case.state.value} rather than detected, "
                "so the drip was never simulated for it",
            )
        )

    if case.state is CaseState.SCHEDULED:
        return SimLane.TARGETED_AGENT, ""

    # An A4 case the agent did not act on. It is still in the experiment, and
    # natural recovery did not stop because we declined to act -- see the module
    # docstring, compromise 4.
    if case.state is CaseState.AWAITING_APPROVAL:
        return (
            SimLane.NATURAL_FLOOR,
            "the case is parked awaiting human approval (§14.2 T2); the simulator "
            "does not grant approvals, so only the natural floor applies",
        )
    return (
        SimLane.NATURAL_FLOOR,
        f"arm A4 case is in {case.state.value}, not scheduled: no action was taken, "
        "so only the natural floor applies",
    )


def _enqueued_verb(conn: Connection, case_id: str) -> ActionType | None:
    """The verb the agent actually put in the outbox for this case.

    Read from the outbox rather than re-derived from the diagnosis: the simulator
    must resolve the action that was *taken*, not the reasoning behind it. Lowest
    row id wins if there are several, so the choice is deterministic; today
    ``flow`` enqueues at most one per case.
    """
    raw = conn.execute(
        sa.select(outbox_table.c.action_type)
        .where(outbox_table.c.case_id == case_id)
        .order_by(outbox_table.c.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    return ActionType(raw) if raw is not None else None


def hedged_contact_enqueued(conn: Connection, case_id: str) -> bool:
    """Whether the contact in the outbox is ``flow``'s contested-dispatch hedge.

    Read from the enqueued envelope's **intent**, not from the case state and not
    from the ``contested_diagnosis_hedged`` audit row. Two reasons, in order:

    * the case state cannot say this at all -- a hedged case and a confidently
      routed one both sit in ``scheduled`` with one ``send_message`` in the
      outbox, so the arm's whole coverage gain would be indistinguishable from
      diagnostic skill;
    * the envelope is the thing that would actually be delivered. If the audit
      row and the outbox ever disagree, the payer receives what is in the outbox,
      and the simulator has to score what the payer receives.

    ``PAYMENT_FAILED_INFORM`` is the marker because it is the only intent
    ``flow`` sends without a resolved cause (``HEDGED_DECLINE_INTENT``). The
    receivable hedge is deliberately *not* counted: it re-uses
    ``PAYMENT_REMINDER``, which is exactly what H9 routes above the floor, so
    there is no weaker message to discount -- see ``flow.hedged_route`` for why
    that branch is a concession rather than a hedge.
    """
    raw = conn.execute(
        sa.select(outbox_table.c.envelope)
        .where(outbox_table.c.case_id == case_id)
        .order_by(outbox_table.c.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if raw is None:
        return False
    envelope = ActionEnvelope.model_validate(json.loads(raw))
    return getattr(envelope.action, "intent", None) is HEDGED_CONTACT_INTENT


def _further_debit_permitted(decline_class: DeclineClass | None) -> bool:
    """Whether another debit attempt on this class has non-zero probability.

    Read off the frozen ``DECLINE_CLASS_META`` table rather than a list kept here:
    retryability is a taxonomy fact with a cited owner, and a second copy of it
    would drift.
    """
    if decline_class is None:
        return True
    return DECLINE_CLASS_META[decline_class].retryability in _FURTHER_DEBIT_OK


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------


def _not_simulated(case: RiskCase, reason: str) -> SimulatedOutcome:
    return SimulatedOutcome(
        case_id=case.case_id,
        arm=case.arm,
        lane=SimLane.NOT_SIMULATED,
        decline_class=case.canonical_decline_class,
        risk_class=case.risk_class,
        amount_at_risk=case.amount_at_risk,
        entry_state=case.state,
        final_state=case.state,
        action_type=None,
        recovered=False,
        recovered_amount=Money.zero(case.amount_at_risk.currency),
        probability=None,
        draw=None,
        simulated_contacts=0,
        reason=reason,
    )


def resolve_case(
    conn: Connection, case_id: str, *, salt: str = SIM_SALT
) -> SimulatedOutcome:
    """Draw and apply one case's simulated outcome.

    Reads the case from the ledger (so it sees whatever ``flow`` left behind), writes
    the ``simulated_psp_response`` row *before* any transition -- the row that
    explains the draw precedes the state changes it caused -- then walks §9.1.
    """
    case = ledger.get_case(conn, case_id)
    if case is None:
        raise ValueError(f"no case {case_id!r} in the ledger")

    lane, why = _lane_for(conn, case)
    if lane is SimLane.NOT_SIMULATED:
        return _not_simulated(case, why)

    verb = _enqueued_verb(conn, case_id) if lane is SimLane.TARGETED_AGENT else None
    hedged = False
    decline_class, risk_class = case.canonical_decline_class, case.risk_class

    if lane in (SimLane.NATURAL_ONLY, SimLane.NATURAL_FLOOR):
        chance = natural_probability(decline_class, risk_class)
        contacts = 0
    elif lane is SimLane.GENERIC_BASELINE:
        chance = generic_probability(decline_class, risk_class)
        contacts = GENERIC_TOUCH_COUNT
    else:
        hedged = hedged_contact_enqueued(conn, case_id)
        chance = targeted_probability(decline_class, risk_class, verb, hedged=hedged)
        contacts = 1 if verb is ActionType.SEND_MESSAGE else 0

    draw = draw_for(case_id, case.arm, salt=salt)
    recovered = draw < chance
    at = case.recovery_window_ends_at

    audit_store.append(
        conn,
        ts=at,
        case_id=case_id,
        # SYSTEM, not AGENT: the environment answered, not the agent. An AGENT row
        # here would attribute the outcome to the thing being measured.
        actor=ActorType.SYSTEM,
        event_type=SIMULATED_RESPONSE_EVENT,
        inputs_digest=digest(
            {
                "case_id": case_id,
                "arm": case.arm.value,
                "lane": lane.value,
                "decline_class": decline_class.value if decline_class else None,
                "risk_class": risk_class.value,
                "action_type": verb.value if verb else None,
                "hedged": hedged,
                "probability": str(chance),
                "draw": str(draw),
                "recovered": recovered,
                "simulated_contacts": contacts,
                "anchors_version": ANCHORS_VERSION,
                "sim_salt": salt,
            }
        ),
        decision_rationale=(
            f"SIMULATED PSP RESPONSE -- reclaim.sim, not a real payment rail. "
            f"arm={case.arm.value} lane={lane.value} "
            f"verb={verb.value if verb else 'none'}"
            f"{' (HEDGED: contested diagnosis, discounted uplift)' if hedged else ''} "
            f"p={chance} draw={draw} -> "
            f"{'recovered' if recovered else 'not recovered'}. "
            f"Probability is an assumption from sim.anchors {ANCHORS_VERSION} "
            f"(see ANCHOR_HONESTY_NOTE), drawn from sha256(salt|case_id|arm)."
        )[:2000],
    )

    final = _apply(conn, case, lane, verb, recovered, at)
    return SimulatedOutcome(
        case_id=case_id,
        arm=case.arm,
        lane=lane,
        decline_class=decline_class,
        risk_class=risk_class,
        amount_at_risk=case.amount_at_risk,
        entry_state=case.state,
        final_state=final,
        action_type=verb,
        recovered=recovered,
        recovered_amount=(
            case.amount_at_risk
            if recovered
            else Money.zero(case.amount_at_risk.currency)
        ),
        probability=chance,
        draw=draw,
        simulated_contacts=contacts,
        hedged=hedged,
        reason=(
            ("recovered inside the window" if recovered else "not recovered inside the window")
            + (f"; natural floor only -- {why}" if lane is SimLane.NATURAL_FLOOR else "")
        ),
    )


def _record_self_heal(
    conn: Connection, case: RiskCase, lane: SimLane, at
) -> CaseState:
    """Move a case that recovered with no action from us, as far as §9.1 permits.

    Three outcomes, in preference order, decided by asking the *frozen* transition
    table rather than by a list kept here:

    ``RECOVERED``  when the edge exists -- only ``escalated`` has one among the
                   states a floor case can be in, and there the state and the money
                   agree with no workaround needed.
    ``STOPPED(already_paid)``  when it does not. ``detected`` and
                   ``awaiting_approval`` land here. For a parked case this is the
                   *only* honest destination: ``awaiting_approval -> scheduled`` is
                   the approve edge, and taking it would be the simulator granting
                   the T2 sign-off §14.2 reserves for a person.
    no move        when the case is already terminal. ``stopped`` has no outgoing
                   edges by construction, so a policy-denied case that later
                   self-heals cannot be moved at all and the amount rides on the
                   ``SimulatedOutcome`` -- compromise 1, reached by a second route.

    The cost of the middle case is that ``STOPPED(already_paid)`` is doing double
    duty: it means both "we never acted and the money arrived" and "we stopped and
    then the money arrived". Only the lane on the audit row tells them apart.
    """
    origin = (
        "control arm (A0) recovered naturally with no action from us"
        if lane is SimLane.NATURAL_ONLY
        else f"recovered naturally from {case.state.value} with no action from us"
    )

    if case.can_transition_to(CaseState.RECOVERED):
        return case_machine.transition(
            conn, case.case_id, CaseState.RECOVERED,
            actor=ActorType.SYSTEM, at=at, event_type="case_recovered",
            rationale=f"simulated: {origin}",
        ).state

    if case.can_transition_to(CaseState.STOPPED):
        return case_machine.transition(
            conn, case.case_id, CaseState.STOPPED,
            actor=ActorType.SYSTEM, at=at, event_type="case_stopped",
            rationale=(
                f"simulated: {origin}; §9.1 offers no "
                f"{case.state.value}->recovered edge, so this is recorded as "
                "already_paid and the amount is carried on the SimulatedOutcome"
            ),
            stop_reason=StopReason.ALREADY_PAID, stopped_at=at,
        ).state

    # Terminal already. Nothing to move; the money is on the outcome.
    return case.state


def _apply(
    conn: Connection,
    case: RiskCase,
    lane: SimLane,
    verb: ActionType | None,
    recovered: bool,
    at,
) -> CaseState:
    """Walk §9.1 to the state this outcome implies, and return it.

    Every edge used here is in ``ALLOWED_CASE_TRANSITIONS``; ``case_machine`` would
    refuse otherwise, which is the point of routing through it rather than updating
    the row.
    """
    stamp = "simulated outcome at the close of the recovery window (sim.anchors)"

    if lane in (SimLane.NATURAL_ONLY, SimLane.NATURAL_FLOOR):
        if not recovered:
            # Nothing happened, so nothing moves. §9.1 has no "window expired
            # unrecovered" terminal and no StopReason for one, so inventing a stop
            # here would put a reason on the row that is not true.
            return case.state
        return _record_self_heal(conn, case, lane, at)

    if lane is SimLane.GENERIC_BASELINE:
        case_machine.transition(
            conn, case.case_id, CaseState.PLANNED,
            actor=ActorType.SYSTEM, at=at,
            rationale=(
                f"simulated A1 baseline: a fixed {GENERIC_TOUCH_COUNT}-touch drip, "
                "not a planned action -- no Plan was built and nothing was enqueued"
            ),
        )
        case_machine.transition(
            conn, case.case_id, CaseState.SCHEDULED, actor=ActorType.SYSTEM,
            at=at, rationale="simulated A1 drip scheduled (no outbox row: simulated)",
        )
        case_machine.transition(
            conn, case.case_id, CaseState.EXECUTING, actor=ActorType.SYSTEM,
            at=at, rationale=stamp,
        )
        if recovered:
            return case_machine.transition(
                conn, case.case_id, CaseState.RECOVERED,
                actor=ActorType.SYSTEM, at=at, event_type="case_recovered",
                rationale="simulated: payment received after the A1 drip",
            ).state
        case_machine.transition(
            conn, case.case_id, CaseState.AWAITING_RESPONSE,
            actor=ActorType.SYSTEM, at=at,
            rationale="simulated: drip delivered, no reply",
        )
        # All four touches spent with no payment: the fixed ladder is exhausted,
        # which §14.3 calls a contact cap. A1 has no next step by construction.
        return case_machine.transition(
            conn, case.case_id, CaseState.STOPPED,
            actor=ActorType.SYSTEM, at=at, event_type="case_stopped",
            rationale=(
                f"simulated: all {GENERIC_TOUCH_COUNT} touches of the fixed A1 drip "
                "were spent with no payment; the baseline ladder has no next step"
            ),
            stop_reason=StopReason.CONTACT_CAP, stopped_at=at,
        ).state

    # TARGETED_AGENT. Entry state is scheduled and one action is in the outbox.
    case_machine.transition(
        conn, case.case_id, CaseState.EXECUTING, actor=ActorType.SYSTEM,
        at=at, rationale=stamp,
    )
    if recovered:
        return case_machine.transition(
            conn, case.case_id, CaseState.RECOVERED,
            actor=ActorType.SYSTEM, at=at, event_type="case_recovered",
            rationale=(
                f"simulated: {verb.value if verb else 'action'} succeeded inside "
                "the recovery window"
            ),
        ).state

    if verb is ActionType.SEND_MESSAGE:
        case_machine.transition(
            conn, case.case_id, CaseState.AWAITING_RESPONSE,
            actor=ActorType.SYSTEM, at=at,
            rationale="simulated: contact delivered, no reply inside the window",
        )
        # §9.1: "no response, cap not hit -> PLANNED (next ladder step)". Unlike A1
        # the agent's ladder is not exhausted -- there is simply no scheduler to run
        # the next step, so the case rests here with its next action owed.
        return case_machine.transition(
            conn, case.case_id, CaseState.PLANNED, actor=ActorType.SYSTEM, at=at,
            rationale=(
                "simulated: no reply and the contact cap is not hit, so the next "
                "ladder step is owed; no scheduler exists to run it (§9.1)"
            ),
        ).state

    if not _further_debit_permitted(case.canonical_decline_class):
        # §9.2 H3/H2: the class needs a new mandate, credential or authentication.
        # Another attempt is 0% and still costs a fee, so this is terminal.
        return case_machine.transition(
            conn, case.case_id, CaseState.STOPPED,
            actor=ActorType.SYSTEM, at=at, event_type="case_stopped",
            rationale=(
                "simulated: the debit was declined again and "
                f"{case.canonical_decline_class.value if case.canonical_decline_class else 'this class'} "
                "permits no further automated attempt -- recovery needs a customer "
                "journey, not a retry (§9.2 H3)"
            ),
            stop_reason=StopReason.HARD_DECLINE_NO_FURTHER_DEBIT, stopped_at=at,
        ).state

    # A soft decline: another attempt is legitimate, there is just no scheduler to
    # make it. Backing off says that without pretending the case is finished.
    return case_machine.transition(
        conn, case.case_id, CaseState.RETRY_BACKOFF, actor=ActorType.SYSTEM, at=at,
        rationale=(
            "simulated: the debit was declined again on a soft class; a further "
            "attempt is permitted but no scheduler exists to make it"
        ),
    ).state


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------


def tally_by_arm(outcomes: Sequence[SimulatedOutcome]) -> dict[Arm, ArmTally]:
    """Per-arm totals, keyed only by the arms actually present, in ``Arm`` order."""
    present = [arm for arm in Arm if any(o.arm is arm for o in outcomes)]
    tallies: dict[Arm, ArmTally] = {}
    for arm in present:
        mine = [o for o in outcomes if o.arm is arm]
        tallies[arm] = ArmTally(
            arm=arm,
            case_count=len(mine),
            simulated_case_count=sum(1 for o in mine if o.was_simulated),
            recovered_case_count=sum(1 for o in mine if o.recovered),
            total_at_risk=money_sum([o.amount_at_risk for o in mine]),
            simulated_at_risk=money_sum(
                [o.amount_at_risk for o in mine if o.was_simulated]
            ),
            gross_recovered=money_sum([o.recovered_amount for o in mine]),
            simulated_contacts=sum(o.simulated_contacts for o in mine),
        )
    return tallies


def resolve_batch(
    conn: Connection, cases: Sequence[RiskCase], *, salt: str = SIM_SALT
) -> BatchResolution:
    """Resolve a whole processed batch, in the order given.

    Takes the cases as ``flow.run`` received them but re-reads each from the ledger,
    so the state it acts on is the one ``flow`` left -- passing stale objects cannot
    make it resolve a case twice. Idempotent in practice for the same reason
    ``_lane_for`` checks the entry state: a second call reports everything as
    ``NOT_SIMULATED`` and writes nothing.
    """
    resolved = tuple(
        resolve_case(conn, case.case_id, salt=salt) for case in cases
    )
    return BatchResolution(outcomes=resolved, by_arm=tally_by_arm(resolved))
