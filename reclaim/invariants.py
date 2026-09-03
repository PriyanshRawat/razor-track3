"""§14.6's ten runtime invariants, checked against what the batch actually persisted.

Read-only. Nothing here writes, and nothing here is wired into
``case_machine.transition``: §14.6 says a violation "halts the case, alerts, and
fails CI", and deciding *where* that halt goes is a separate call. This module
answers one question -- "does the state on disk satisfy invariant N, and can that
even be determined?" -- and answers it about a whole batch after the fact.

Why a fourth status exists
--------------------------
The obvious shape for this module is ten booleans. That shape is a lie here.
Four of §14.6's ten invariants talk about state this repository does not persist:
there is no consent store, no holds table, no mandate table, no pre-debit
notification log, no incident/cohort table and no concession ledger. Against that
schema a naive checker returns ten greens, and the greens for #2, #3, #4, #5 and
#7 mean only "no row matched a query that no row can ever match".

So a result is one of four things, and only the first is a pass:

* ``HOLDS`` -- the check ran against rows that exist, and found no breach.
* ``VIOLATED`` -- the check ran and found a breach; the case ids are named.
* ``VACUOUS`` -- the check is fully expressible against persisted state and did
  run, but zero candidate rows exist, so the pass carries no information. #10 is
  the standing example: nothing in Phase 1 ever marks a case suppressed, so
  "no suppressed case was contacted" is true of an empty set.
* ``NOT_CHECKABLE`` -- the state the invariant is *about* has no home in the
  schema. No amount of data makes this one green.

``InvariantReport.batch_passes`` is True only when all ten ``HOLDS``. An
unverifiable invariant is not a pass, and that is the whole design: the cost of
the softer rule is that a demo scoreboard reading "10/10 green" would be
unearned, and this project has already lost once to a green suite that measured
only the code that had tests.

The asymmetry that keeps the unverifiable checks useful
-------------------------------------------------------
Two of the five ``NOT_CHECKABLE`` invariants still run a scan, because a
*positive* finding is sound even when a negative one is not. Evidence of a breach
is evidence; absence of a breach in a table that cannot record one is not. So #2
and #4 may return ``VIOLATED`` and may never return ``HOLDS``. #3, #5 and #7 have
no positive path at all -- with no send instant, no mandate cap and no recorded
concession value, no query over this schema can even find a breach -- and they
return ``NOT_CHECKABLE`` unconditionally.

Why the raw JSON and not the models
-----------------------------------
Rows are read with ``json.loads`` over the ``data``/``envelope`` columns rather
than through ``ledger.get_case`` / ``codec.decode_model``. The contracts refuse to
*construct* several of the states this module exists to find (``Obligation``
rejects collected > gross; ``ActionEnvelope`` rejects a tampered idempotency key),
so a validating read raises on exactly the row the checker was written to report.
A checker that crashes on bad data reports nothing about it. The cost is that this
module duplicates a little field knowledge (``amount.paise``, ``action.rail``);
that knowledge is pinned by the tests, which build their rows through the models.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from reclaim.contracts.actions import ACTION_SPECS, ActionType
from reclaim.contracts.enums import CaseState
from reclaim.spine.tables import (
    TERMINAL_STATE_VALUES,
    audit_log,
    obligations,
    outbox as outbox_table,
    risk_cases,
)

__all__ = [
    "CHECKS",
    "INVARIANT_TEXT",
    "InvariantReport",
    "InvariantResult",
    "InvariantStatus",
    "check_all",
]


class InvariantStatus(StrEnum):
    """What the checker is entitled to say about one invariant. See the module
    docstring: only ``HOLDS`` is a pass."""

    HOLDS = "holds"
    VIOLATED = "violated"
    VACUOUS = "vacuous"
    NOT_CHECKABLE = "not_checkable"

    @property
    def is_pass(self) -> bool:
        return self is InvariantStatus.HOLDS


#: §14.6, verbatim. A test parses the same ten sentences out of HACKATHON_PLAN.md
#: and compares, so a reworded plan breaks the build rather than the wording here
#: drifting into a paraphrase of an invariant nobody agreed to.
INVARIANT_TEXT: Mapping[int, str] = {
    1: "No double debit for the same obligation-attempt (idempotency key uniqueness).",
    2: "No contact after opt-out. Ever.",
    3: "No contact outside quiet hours, in any timezone.",
    4: "No debit without a valid mandate and a satisfied pre-debit notification window.",
    5: "No debit exceeding the mandate cap.",
    6: "Total recovered per obligation ≤ amount owed.",
    7: "Agent-granted concession value = ₹0.",
    8: "Every external action has exactly one audit row and one idempotency key.",
    9: (
        "Every non-terminal case has exactly one scheduled next action or one open "
        "human task."
    ),
    10: "No suppressed-cohort case emits customer contact.",
}


@dataclass(frozen=True)
class InvariantResult:
    """One invariant's verdict, and enough context to argue with it.

    ``candidates_examined`` is the denominator, and it is not decoration: a
    ``HOLDS`` over zero candidates is the laundered green this module exists to
    refuse, so the constructor rejects it.
    """

    number: int
    text: str
    status: InvariantStatus
    detail: str
    candidates_examined: int = 0
    offending_case_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is InvariantStatus.HOLDS and self.candidates_examined == 0:
            raise ValueError(
                f"invariant #{self.number} cannot hold over zero candidates; that "
                "is a vacuous pass and the status for it is VACUOUS"
            )
        if self.status is InvariantStatus.VIOLATED and not self.offending_case_ids:
            raise ValueError(
                f"invariant #{self.number} was reported violated without naming a "
                "case; a violation nobody can look up is not actionable"
            )

    @property
    def is_pass(self) -> bool:
        return self.status.is_pass

    def __str__(self) -> str:
        cases = ""
        if self.offending_case_ids:
            shown = ", ".join(self.offending_case_ids[:5])
            more = len(self.offending_case_ids) - 5
            cases = f" [{shown}{f', +{more} more' if more > 0 else ''}]"
        return (
            f"#{self.number:<2} {self.status.value.upper():<14} "
            f"n={self.candidates_examined:<5} {self.text}{cases}\n"
            f"        {self.detail}"
        )


@dataclass(frozen=True)
class InvariantReport:
    """All ten verdicts for one batch. Iterable, so ``for r in check_all(conn)``
    prints the report; ``batch_passes`` is the aggregate a CI gate reads."""

    results: tuple[InvariantResult, ...]

    def __iter__(self):
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __getitem__(self, number: int) -> InvariantResult:
        """Indexed by **invariant number**, not by position: ``report[9]`` is
        §14.6 #9. Positional indexing of a fixed ten-row table is how an
        off-by-one gets read as a different invariant."""
        for result in self.results:
            if result.number == number:
                return result
        raise KeyError(f"no invariant #{number}")

    @property
    def batch_passes(self) -> bool:
        """True only when all ten genuinely hold. Unverifiable is not a pass."""
        return all(r.is_pass for r in self.results)

    @property
    def violations(self) -> tuple[InvariantResult, ...]:
        return tuple(r for r in self.results if r.status is InvariantStatus.VIOLATED)

    @property
    def unverifiable(self) -> tuple[InvariantResult, ...]:
        """The ones a reader must not count as green -- vacuous or unpersisted."""
        return tuple(
            r
            for r in self.results
            if r.status in (InvariantStatus.VACUOUS, InvariantStatus.NOT_CHECKABLE)
        )

    def counts(self) -> Mapping[InvariantStatus, int]:
        return {
            status: sum(1 for r in self.results if r.status is status)
            for status in InvariantStatus
        }

    def summary(self) -> str:
        counted = ", ".join(
            f"{status.value}={n}" for status, n in self.counts().items() if n
        )
        return f"batch_passes={self.batch_passes} ({counted})"


# ---------------------------------------------------------------------------
# Derived vocabularies
# ---------------------------------------------------------------------------

#: The verbs that put a message in front of a customer. Derived from
#: ``ACTION_SPECS`` rather than listed: ``is_outbound_contact`` is the catalog's
#: own answer to "is this contact?", and a new contact verb must not need a second
#: edit here to be covered by invariants #2, #3 and #10.
CONTACT_ACTION_TYPES: frozenset[ActionType] = frozenset(
    action_type
    for action_type, spec in ACTION_SPECS.items()
    if spec.is_outbound_contact
)

#: The verbs that could grant a customer something of value. **Listed, not
#: derived**: the catalog's ``FINANCIAL_AUTHORITY`` category also covers
#: ``schedule_debit`` and ``propose_route_change``, neither of which is a
#: concession, so deriving from it would silently widen the set. A test walks all
#: thirteen verbs against this classification.
CONCESSION_ACTION_TYPES: frozenset[ActionType] = frozenset(
    {
        ActionType.OFFER_PAYMENT_PLAN,
        ActionType.APPLY_GRACE_PERIOD,
        ActionType.RECOMMEND_WRITE_OFF,
    }
)

#: States in which §9.1 says a human owns the case. Invariant #9 accepts these as
#: "one open human task" -- and that acceptance is a **tautology**, because there
#: is no human-task table: the state is both the claim and its evidence. Cases
#: satisfied this way are counted separately and never make #9 green on their own.
HUMAN_TASK_PROXY_STATES: frozenset[CaseState] = frozenset(
    {CaseState.AWAITING_APPROVAL, CaseState.ESCALATED}
)

#: Outbox statuses that mean the action is still going to happen.
_OPEN_OUTBOX_STATUSES: frozenset[str] = frozenset({"pending", "claimed"})


def check_all(conn: Connection) -> InvariantReport:
    """Evaluate all ten invariants against the persisted batch, in plan order."""
    return InvariantReport(tuple(CHECKS[n](conn) for n in sorted(CHECKS)))


# ---------------------------------------------------------------------------
# Shared reads. Raw columns and raw JSON -- see the module docstring.
# ---------------------------------------------------------------------------


def _outbox_rows(conn: Connection, *, action_types: frozenset[ActionType] | None = None):
    """Outbox rows, optionally narrowed to a set of verbs, oldest first."""
    query = sa.select(
        outbox_table.c.id,
        outbox_table.c.case_id,
        outbox_table.c.idempotency_key,
        outbox_table.c.obligation_id,
        outbox_table.c.attempt_sequence,
        outbox_table.c.action_type,
        outbox_table.c.status,
        outbox_table.c.completed_at,
        outbox_table.c.envelope,
    ).order_by(outbox_table.c.id)
    if action_types is not None:
        query = query.where(
            outbox_table.c.action_type.in_(sorted(a.value for a in action_types))
        )
    return conn.execute(query).all()


def _action_of(row) -> dict:
    """The action parameters of an outbox row, from the envelope JSON.

    The envelope is the source of truth (``codec``); the columns shadow it. A
    check that reads the columns is checking the shadow, and the shadow is what a
    writer that skipped ``outbox.enqueue`` gets wrong.
    """
    return json.loads(row.envelope).get("action", {})


# ---------------------------------------------------------------------------
# #1 -- No double debit for the same obligation-attempt
# ---------------------------------------------------------------------------


def check_no_double_debit(conn: Connection) -> InvariantResult:
    """Two enqueued debits for one ``(obligation, attempt)`` pair, or one key twice.

    Genuinely checkable: the outbox persists both coordinates. The database already
    forbids this twice over (``outbox.idempotency_key`` UNIQUE and the partial
    ``uq_outbox_obligation_attempt``), so this is a re-derivation rather than the
    first line of defence -- and it is worth having because the index protects only
    rows whose ``obligation_id`` column is populated, which is a property of the
    writer, not of the schema.
    """
    rows = _outbox_rows(conn, action_types=frozenset({ActionType.SCHEDULE_DEBIT}))

    by_attempt: dict[tuple, list[str]] = {}
    by_key: dict[str, list[str]] = {}
    unprotected = 0
    for row in rows:
        action = _action_of(row)
        coords = (action.get("obligation_id"), action.get("attempt_sequence"))
        by_attempt.setdefault(coords, []).append(row.case_id)
        by_key.setdefault(row.idempotency_key, []).append(row.case_id)
        if (row.obligation_id, row.attempt_sequence) != coords:
            unprotected += 1

    duplicate_attempts = {k: v for k, v in by_attempt.items() if len(v) > 1}
    duplicate_keys = {k: v for k, v in by_key.items() if len(v) > 1}

    if duplicate_attempts or duplicate_keys:
        offenders = sorted(
            {case_id for ids in duplicate_attempts.values() for case_id in ids}
            | {case_id for ids in duplicate_keys.values() for case_id in ids}
        )
        pairs = "; ".join(
            f"{obligation_id} attempt {attempt} -> {', '.join(sorted(set(ids)))}"
            for (obligation_id, attempt), ids in sorted(
                duplicate_attempts.items(), key=lambda kv: str(kv[0])
            )
        )
        return InvariantResult(
            number=1,
            text=INVARIANT_TEXT[1],
            status=InvariantStatus.VIOLATED,
            candidates_examined=len(rows),
            offending_case_ids=tuple(offenders),
            detail=(
                f"{len(duplicate_attempts)} obligation-attempt pair(s) carry more "
                f"than one enqueued debit ({pairs or 'n/a'}); "
                f"{len(duplicate_keys)} idempotency key(s) appear twice. "
                f"{unprotected} of {len(rows)} debit rows have shadow columns that "
                "disagree with their envelope, which is how such a row gets past "
                "uq_outbox_obligation_attempt."
            ),
        )

    if not rows:
        return InvariantResult(
            number=1,
            text=INVARIANT_TEXT[1],
            status=InvariantStatus.VACUOUS,
            candidates_examined=0,
            detail=(
                "no debit has been enqueued in this batch, so there is nothing to "
                "double. This check acquires meaning only once a schedule_debit "
                "row reaches the outbox."
            ),
        )

    return InvariantResult(
        number=1,
        text=INVARIANT_TEXT[1],
        status=InvariantStatus.HOLDS,
        candidates_examined=len(rows),
        detail=(
            f"{len(rows)} enqueued debits over {len(by_attempt)} distinct "
            f"(obligation, attempt) pairs and {len(by_key)} distinct idempotency "
            f"keys. Coordinates are read from each row's envelope JSON rather than "
            f"its columns; {unprotected} row(s) have columns that disagree with the "
            "envelope and are therefore not covered by the partial UNIQUE index."
        ),
    )


# ---------------------------------------------------------------------------
# #8 -- Every external action has exactly one audit row and one idempotency key
# ---------------------------------------------------------------------------


def check_action_audit_pairing(conn: Connection) -> InvariantResult:
    """Pair every outbox row with the audit row that authorised it.

    "External action" is read as *an action enqueued for external execution*,
    because there is no executor yet: the outbox row is the only record that an
    action was authorised at all. That reading is the one limitation worth stating
    -- when an executor lands it will legitimately append further rows about the
    same action (claimed, result), and the "exactly one audit row" half of #14.6
    will need a decision about which row it means. Until then, a second row for one
    key is a genuine double-record and is reported.

    Three failure directions, all persisted and all checked:

    * an outbox row with no audit row -- an action nobody can explain;
    * one key across two audit rows -- one act recorded as two;
    * an audit row whose key is in no outbox row -- an act nothing can execute or
      reconcile.
    """
    rows = _outbox_rows(conn)
    audited = conn.execute(
        sa.select(audit_log.c.case_id, audit_log.c.event_type, audit_log.c.idempotency_key)
        .where(audit_log.c.idempotency_key.isnot(None))
        .order_by(audit_log.c.sequence)
    ).all()

    audit_by_key: dict[str, list] = {}
    for row in audited:
        audit_by_key.setdefault(row.idempotency_key, []).append(row)

    outbox_by_key: dict[str, list[str]] = {}
    for row in rows:
        outbox_by_key.setdefault(row.idempotency_key, []).append(row.case_id)

    unaudited = [row.case_id for row in rows if row.idempotency_key not in audit_by_key]
    over_audited = sorted(
        key
        for key, entries in audit_by_key.items()
        if len(entries) > 1 and key in outbox_by_key
    )
    orphan_audit = sorted(set(audit_by_key) - set(outbox_by_key))
    duplicate_keys = sorted(key for key, ids in outbox_by_key.items() if len(ids) > 1)

    if unaudited or over_audited or orphan_audit or duplicate_keys:
        offenders = set(unaudited)
        for key in over_audited + duplicate_keys:
            offenders.update(outbox_by_key.get(key, ()))
        for key in orphan_audit:
            offenders.update(
                entry.case_id for entry in audit_by_key[key] if entry.case_id
            )
        return InvariantResult(
            number=8,
            text=INVARIANT_TEXT[8],
            status=InvariantStatus.VIOLATED,
            candidates_examined=len(rows),
            offending_case_ids=tuple(sorted(offenders)),
            detail=(
                f"{len(unaudited)} unaudited outbox row(s); "
                f"{len(over_audited)} action(s) carrying more than one audit row; "
                f"{len(orphan_audit)} audit key(s) with no outbox row "
                f"({', '.join(orphan_audit[:3]) or 'none'}); "
                f"{len(duplicate_keys)} idempotency key(s) on more than one outbox row."
            ),
        )

    if not rows:
        return InvariantResult(
            number=8,
            text=INVARIANT_TEXT[8],
            status=InvariantStatus.VACUOUS,
            candidates_examined=0,
            detail=(
                f"no action has been enqueued, so there is nothing to pair. "
                f"{len(audit_by_key)} audit row(s) carry an idempotency key."
            ),
        )

    return InvariantResult(
        number=8,
        text=INVARIANT_TEXT[8],
        status=InvariantStatus.HOLDS,
        candidates_examined=len(rows),
        detail=(
            f"{len(rows)} enqueued action(s), each with exactly one audit row "
            f"carrying its key, and {len(outbox_by_key)} distinct keys. No audit "
            "row claims an action the outbox never carried. Note that 'external' "
            "here means 'enqueued for execution': there is no executor, so no row "
            "records a send that actually happened."
        ),
    )


# ---------------------------------------------------------------------------
# #9 -- Every non-terminal case has exactly one scheduled next action or one
#       open human task
# ---------------------------------------------------------------------------


def check_no_orphaned_live_case(conn: Connection) -> InvariantResult:
    """§9.1's "no case can be silently orphaned", read off the two tables.

    A live case is satisfied by exactly one *open* outbox row (pending or claimed).
    Failing that, a case in one of ``HUMAN_TASK_PROXY_STATES`` is treated as having
    an open human task -- and that treatment is circular, because the state is the
    only evidence there is. So the proxy can never make this invariant green: if no
    live case is satisfied by a real queued action, the verdict is ``VACUOUS`` and
    the proxy count is stated.

    Two ways to fail, both real: nothing queued and no human queue (orphaned), or
    more than one queued action on one live case.
    """
    cases = conn.execute(
        sa.select(risk_cases.c.case_id, risk_cases.c.state)
        .where(risk_cases.c.state.notin_(TERMINAL_STATE_VALUES))
        .order_by(risk_cases.c.case_id)
    ).all()

    open_counts = dict(
        conn.execute(
            sa.select(outbox_table.c.case_id, sa.func.count())
            .where(outbox_table.c.status.in_(sorted(_OPEN_OUTBOX_STATUSES)))
            .group_by(outbox_table.c.case_id)
        ).all()
    )

    proxy_values = frozenset(state.value for state in HUMAN_TASK_PROXY_STATES)
    scheduled, proxied, orphaned, over_scheduled = [], [], [], []
    for case in cases:
        queued = int(open_counts.get(case.case_id, 0))
        if queued == 1:
            scheduled.append(case.case_id)
        elif queued > 1:
            over_scheduled.append(case.case_id)
        elif case.state in proxy_values:
            proxied.append(case.case_id)
        else:
            orphaned.append(case.case_id)

    by_state = ", ".join(
        f"{state}={sum(1 for c in cases if c.state == state)}"
        for state in sorted({c.state for c in cases})
    )

    if orphaned or over_scheduled:
        return InvariantResult(
            number=9,
            text=INVARIANT_TEXT[9],
            status=InvariantStatus.VIOLATED,
            candidates_examined=len(cases),
            offending_case_ids=tuple(sorted(orphaned + over_scheduled)),
            detail=(
                f"{len(orphaned)} of {len(cases)} non-terminal case(s) are orphaned "
                f"-- no open outbox row and no human-owned state -- and "
                f"{len(over_scheduled)} carry more than one open action. Live states "
                f"present: {by_state}. {len(proxied)} case(s) are counted as having "
                "an open human task purely on their state, which is not independent "
                "evidence."
            ),
        )

    if not scheduled:
        return InvariantResult(
            number=9,
            text=INVARIANT_TEXT[9],
            status=InvariantStatus.VACUOUS,
            candidates_examined=len(cases),
            detail=(
                f"{len(cases)} non-terminal case(s), none of which has a queued "
                f"action; {len(proxied)} are parked in a human-owned state "
                f"({', '.join(sorted(proxy_values))}). There is no human-task table, "
                "so that parking is the claim and its only evidence -- a pass here "
                "would be circular."
            ),
        )

    return InvariantResult(
        number=9,
        text=INVARIANT_TEXT[9],
        status=InvariantStatus.HOLDS,
        candidates_examined=len(cases),
        detail=(
            f"{len(scheduled)} of {len(cases)} non-terminal case(s) have exactly one "
            f"open outbox row; the other {len(proxied)} are parked in a human-owned "
            "state, which is accepted on the state alone (no human-task table "
            "exists) and is therefore not independent evidence. Live states: "
            f"{by_state}."
        ),
    )


# ---------------------------------------------------------------------------
# #6 -- Total recovered per obligation <= amount owed
# ---------------------------------------------------------------------------


def check_recovery_within_amount_owed(conn: Connection) -> InvariantResult:
    """Two independent readings of "recovered", compared against gross separately.

    * **Settled**, from the obligation itself: partial payments plus credit notes.
      This is what ``Obligation.outstanding`` is built on.
    * **Recognised**, from the ledger: the amount at risk of every case on the
      obligation that reached ``RECOVERED``. §13 recognises at-risk once per case,
      and the ledger permits a second case once the first is terminal, so two
      recovered cases on one obligation double-count it -- a defect neither case
      can show on its own.

    They are compared to gross **separately, never summed**: nothing writes both
    today, and adding them the day something does would invent an over-collection
    that never happened. The cost of that choice is stated rather than hidden -- a
    real system that recorded a partial payment *and* recovered the residual on a
    second case would need this check rewritten around one definition of recovered.

    Two assumptions, both unverifiable from this schema and both load-bearing:
    a ``RECOVERED`` case is credited with its **whole** amount at risk (nothing
    persists a per-case recovered amount), and ``PARTIALLY_RECOVERED`` cases are
    credited with **nothing** (same reason) -- so a batch that partially recovers a
    lot of money is under-counted here, and the count of such cases is reported.
    Amounts are summed in paise without a currency check because ``Currency`` has
    exactly one member; a second member makes that unsound.
    """
    obligation_rows = conn.execute(
        sa.select(
            obligations.c.obligation_id,
            obligations.c.gross_amount_paise,
            obligations.c.data,
        ).order_by(obligations.c.obligation_id)
    ).all()
    case_rows = conn.execute(
        sa.select(
            risk_cases.c.case_id,
            risk_cases.c.obligation_id,
            risk_cases.c.state,
            risk_cases.c.amount_at_risk_paise,
        ).order_by(risk_cases.c.case_id)
    ).all()

    recognised: dict[str, int] = {}
    recovered_cases: dict[str, list[str]] = {}
    partial_cases = 0
    for case in case_rows:
        if case.state == CaseState.PARTIALLY_RECOVERED.value:
            partial_cases += 1
        if case.state != CaseState.RECOVERED.value:
            continue
        recognised[case.obligation_id] = (
            recognised.get(case.obligation_id, 0) + int(case.amount_at_risk_paise)
        )
        recovered_cases.setdefault(case.obligation_id, []).append(case.case_id)

    candidates: list[str] = []
    breaches: list[str] = []
    offenders: set[str] = set()
    for row in obligation_rows:
        stored = json.loads(row.data)
        settled = sum(
            int(p["amount"]["paise"]) for p in stored.get("partial_payments") or ()
        ) + sum(int(c["amount"]["paise"]) for c in stored.get("credit_notes") or ())
        claimed = recognised.get(row.obligation_id, 0)
        gross = int(row.gross_amount_paise)
        if settled == 0 and claimed == 0:
            continue
        candidates.append(row.obligation_id)
        if settled > gross:
            breaches.append(
                f"{row.obligation_id}: settled {settled}p over gross {gross}p"
            )
            offenders.update(recovered_cases.get(row.obligation_id, ()))
        if claimed > gross:
            names = ", ".join(sorted(recovered_cases.get(row.obligation_id, ())))
            breaches.append(
                f"{row.obligation_id}: {len(recovered_cases.get(row.obligation_id, ()))} "
                f"recovered case(s) ({names}) recognise {claimed}p against gross {gross}p"
            )
            offenders.update(recovered_cases.get(row.obligation_id, ()))

    if breaches:
        return InvariantResult(
            number=6,
            text=INVARIANT_TEXT[6],
            status=InvariantStatus.VIOLATED,
            candidates_examined=len(candidates),
            # An over-collected obligation with no recovered case names no case at
            # all; the obligation id is in the detail, and a result must name
            # something, so the obligation stands in for it.
            offending_case_ids=tuple(sorted(offenders) or [b.split(":")[0] for b in breaches]),
            detail=(
                f"{len(breaches)} obligation(s) collect more than is owed: "
                + "; ".join(breaches[:5])
                + f". {partial_cases} partially-recovered case(s) contribute nothing "
                "to this sum because no per-case recovered amount is persisted."
            ),
        )

    if not candidates:
        return InvariantResult(
            number=6,
            text=INVARIANT_TEXT[6],
            status=InvariantStatus.VACUOUS,
            candidates_examined=0,
            detail=(
                f"none of {len(obligation_rows)} obligation(s) has any recovery "
                "recorded -- no partial payment, no credit note, no case in "
                "'recovered'. The sum is empty, so the comparison passes without "
                "testing anything. A batch that has run its outcomes gives this "
                "check a denominator."
            ),
        )

    return InvariantResult(
        number=6,
        text=INVARIANT_TEXT[6],
        status=InvariantStatus.HOLDS,
        candidates_examined=len(candidates),
        detail=(
            f"{len(candidates)} of {len(obligation_rows)} obligation(s) carry "
            f"recovery ({len(recognised)} via a recovered case, "
            f"{len(candidates) - len(recognised)} via settlement rows only); none "
            f"exceeds its gross amount. {partial_cases} partially-recovered case(s) "
            "are credited with nothing, because no per-case recovered amount is "
            "persisted -- this check under-counts rather than guesses."
        ),
    )


def _stub(number: int) -> Callable[[Connection], InvariantResult]:
    def _check(conn: Connection) -> InvariantResult:
        return InvariantResult(
            number=number,
            text=INVARIANT_TEXT[number],
            status=InvariantStatus.NOT_CHECKABLE,
            detail="not implemented yet",
        )

    return _check


CHECKS: Mapping[int, Callable[[Connection], InvariantResult]] = {
    1: check_no_double_debit,
    6: check_recovery_within_amount_owed,
    8: check_action_audit_pairing,
    9: check_no_orphaned_live_case,
    **{n: _stub(n) for n in INVARIANT_TEXT if n not in (1, 6, 8, 9)},
}

_missing = set(INVARIANT_TEXT) - set(CHECKS)
if _missing:  # pragma: no cover - import-time guard
    raise RuntimeError(
        f"§14.6 invariants {sorted(_missing)} have wording but no check; a listed "
        "invariant with no checker is the shape of a green that means nothing"
    )
