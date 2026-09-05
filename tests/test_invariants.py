"""The §14.6 runtime-invariant checker, and the honesty of its verdicts.

Two kinds of test live here, and the second kind is the point.

The ordinary kind: build a batch that satisfies an invariant and assert the
checker says so; build one that breaks it and assert the checker names the cases.
A checker that only ever sees clean data is untested.

The load-bearing kind: assert what the checker is **not** allowed to say. Four of
§14.6's ten invariants describe state this repository does not persist -- there is
no consent store, no holds table, no mandate table, no notification log, no
incident table and no concession ledger. A checker that reported ten greens over
that would launder absence of evidence into evidence of compliance, which is the
exact failure this project has already paid for once. So the tests below pin the
status of every invariant on an empty database and on a full one, count each
status, and assert that no database state can turn a structurally unverifiable
invariant green.

The ten sentences are not transcribed here by hand. They are parsed out of
HACKATHON_PLAN.md §14.6 at test time, so the module's table is pinned to the plan
rather than to a copy of the plan that can drift.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from reclaim import invariants
from reclaim.contracts.enums import ActorType, CaseState, StopReason
from reclaim.contracts.money import Money
from reclaim.contracts.temporal import to_rfc3339
from reclaim.invariants import InvariantStatus
from reclaim.spine import audit_store, case_machine, ledger, outbox
from reclaim.spine.tables import outbox as outbox_table

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _plan_invariant_text() -> dict[int, str]:
    """The ten sentences of §14.6, read out of the plan itself.

    Re-derived from the source of authority rather than restated: a test that
    holds its own copy of the table cannot notice the plan changing under it.
    """
    plan = (_REPO_ROOT / "HACKATHON_PLAN.md").read_text(encoding="utf-8")
    section = plan.split("### 14.6", 1)[1]
    found: dict[int, str] = {}
    for line in section.splitlines():
        match = re.fullmatch(r"(\d+)\. (.+)", line.strip())
        if match:
            found[int(match.group(1))] = match.group(2)
        elif found and not line.strip():
            continue
        elif found:
            break
    return found


def test_the_plan_parser_finds_exactly_ten_numbered_invariants():
    """Guards the guard: if §14.6 is renumbered this test file breaks loudly
    rather than silently comparing the module against an empty dict."""
    text = _plan_invariant_text()
    assert sorted(text) == list(range(1, 11))
    assert text[1].startswith("No double debit")
    assert text[10] == "No suppressed-cohort case emits customer contact."


def test_check_all_reports_every_one_of_the_ten_plan_invariants(conn):
    report = invariants.check_all(conn)
    assert [r.number for r in report] == list(range(1, 11))
    assert {r.number: r.text for r in report} == _plan_invariant_text()


def test_an_empty_database_proves_nothing_and_does_not_pass(conn):
    """The single most important assertion in this file.

    An empty batch satisfies every invariant vacuously. If ``batch_passes`` were
    True here, the checker would certify a database with no data in it.
    """
    report = invariants.check_all(conn)
    assert not report.batch_passes
    assert [r for r in report if r.status is InvariantStatus.HOLDS] == []


# ---------------------------------------------------------------------------
# Batch builders. These go through the spine's public API wherever a real writer
# would, and drop to raw SQL only where the point is a writer that did not.
# ---------------------------------------------------------------------------

#: One fixed instant for the whole file. A constant rather than ``utcnow()``:
#: two writes that must be comparable cannot depend on how long the test took.
_STAMP = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
_STAMP_TEXT = to_rfc3339(_STAMP)


def _open_case(conn, make_obligation, make_case, idx, **case_over):
    """Persist obligation ``obl_{idx}`` and a case on it, both via the ledger."""
    obligation = make_obligation(
        obligation_id=f"obl_{idx}",
        payer_id=f"payer_{idx}",
        gross_amount=case_over.pop("gross_amount", Money.from_rupees(1499)),
    )
    ledger.upsert_obligation(conn, obligation)
    case = make_case(
        case_id=f"case_{idx}",
        obligation_id=obligation.obligation_id,
        payer_id=obligation.payer_id,
        **case_over,
    )
    return ledger.open_case(conn, case)


def _enqueue_with_audit(conn, envelope):
    """What ``flow._allow`` does: one outbox row, one audit row carrying the key."""
    item = outbox.enqueue(conn, envelope, at=_STAMP)
    audit_store.append(
        conn,
        ts=_STAMP,
        case_id=envelope.case_id,
        actor=ActorType.AGENT,
        event_type="action_scheduled",
        inputs_digest="0" * 64,
        idempotency_key=envelope.idempotency_key,
        decision_rationale="test batch",
    )
    return item


def _raw_outbox_insert(conn, envelope, **columns):
    """Insert an outbox row **without** going through ``outbox.enqueue``.

    The queryable columns are exactly what a careless writer gets wrong, and the
    partial UNIQUE index that stops a cross-case double debit only bites when
    ``obligation_id`` is populated. So this is how invariant #1's remaining hole
    is reachable, and therefore how it must be testable.
    """
    values = dict(
        idempotency_key=envelope.idempotency_key,
        case_id=envelope.case_id,
        obligation_id=None,
        attempt_sequence=None,
        action_type=envelope.action.action_type.value,
        envelope=envelope.model_dump_json(),
        status="pending",
        created_at=_STAMP_TEXT,
        updated_at=_STAMP_TEXT,
    )
    values.update(columns)
    conn.execute(outbox_table.insert().values(**values))


# ---------------------------------------------------------------------------
# #1 -- no double debit for the same obligation-attempt
# ---------------------------------------------------------------------------


def test_one_debit_per_obligation_attempt_holds(
    conn, make_obligation, make_case, make_debit_envelope
):
    for idx in (1, 2):
        _open_case(conn, make_obligation, make_case, idx)
        _enqueue_with_audit(
            conn,
            make_debit_envelope(
                case_id=f"case_{idx}", action_id=f"act_{idx}", obligation_id=f"obl_{idx}"
            ),
        )

    result = invariants.check_no_double_debit(conn)
    assert result.status is InvariantStatus.HOLDS
    assert result.candidates_examined == 2


def test_two_cases_debiting_one_obligation_attempt_is_caught(
    conn, make_obligation, make_case, make_debit_envelope
):
    """CONTRACTS.md Q1, re-opened by a writer that skips ``outbox.enqueue``.

    The two envelopes derive *different* idempotency keys because their case ids
    differ, so the UNIQUE key does not stop them; leaving the shadow columns NULL
    dodges the partial index. The coordinates are still in the envelope JSON,
    which is why the checker reads that and not the columns.
    """
    _open_case(conn, make_obligation, make_case, 1)
    for case_id in ("case_1", "case_9"):
        _raw_outbox_insert(
            conn,
            make_debit_envelope(
                case_id=case_id,
                action_id="act_x",
                obligation_id="obl_1",
                attempt_sequence=1,
            ),
        )

    result = invariants.check_no_double_debit(conn)
    assert result.status is InvariantStatus.VIOLATED
    assert set(result.offending_case_ids) == {"case_1", "case_9"}
    assert "obl_1" in result.detail


def test_no_debits_at_all_is_vacuous_not_a_pass(conn):
    result = invariants.check_no_double_debit(conn)
    assert result.status is InvariantStatus.VACUOUS
    assert not result.is_pass


# ---------------------------------------------------------------------------
# #8 -- one audit row and one idempotency key per external action
# ---------------------------------------------------------------------------


def test_every_enqueued_action_with_its_audit_row_holds(
    conn, make_obligation, make_case, make_debit_envelope
):
    for idx in (1, 2):
        _open_case(conn, make_obligation, make_case, idx)
        _enqueue_with_audit(
            conn,
            make_debit_envelope(
                case_id=f"case_{idx}", action_id=f"act_{idx}", obligation_id=f"obl_{idx}"
            ),
        )

    result = invariants.check_action_audit_pairing(conn)
    assert result.status is InvariantStatus.HOLDS
    assert result.candidates_examined == 2


def test_an_enqueued_action_with_no_audit_row_is_caught(
    conn, make_obligation, make_case, make_debit_envelope
):
    """The failure mode that matters: something reaches the outbox -- from where an
    executor will put it in front of a customer -- and nothing in the chain says
    who decided it or why."""
    _open_case(conn, make_obligation, make_case, 1)
    outbox.enqueue(
        conn,
        make_debit_envelope(case_id="case_1", action_id="act_1", obligation_id="obl_1"),
        at=_STAMP,
    )

    result = invariants.check_action_audit_pairing(conn)
    assert result.status is InvariantStatus.VIOLATED
    assert result.offending_case_ids == ("case_1",)
    assert "unaudited" in result.detail


def test_one_action_audited_twice_is_caught(
    conn, make_obligation, make_case, make_debit_envelope
):
    _open_case(conn, make_obligation, make_case, 1)
    envelope = make_debit_envelope(
        case_id="case_1", action_id="act_1", obligation_id="obl_1"
    )
    _enqueue_with_audit(conn, envelope)
    audit_store.append(
        conn,
        ts=_STAMP,
        case_id="case_1",
        actor=ActorType.AGENT,
        event_type="action_scheduled",
        inputs_digest="1" * 64,
        idempotency_key=envelope.idempotency_key,
        decision_rationale="the same action, recorded a second time",
    )

    result = invariants.check_action_audit_pairing(conn)
    assert result.status is InvariantStatus.VIOLATED
    assert result.offending_case_ids == ("case_1",)


def test_an_audited_action_with_no_outbox_row_is_caught(
    conn, make_obligation, make_case, make_debit_envelope
):
    """The chain claims an external action that the outbox never carried, so
    nothing can say whether it executed."""
    _open_case(conn, make_obligation, make_case, 1)
    _enqueue_with_audit(
        conn,
        make_debit_envelope(case_id="case_1", action_id="act_1", obligation_id="obl_1"),
    )
    audit_store.append(
        conn,
        ts=_STAMP,
        case_id="case_1",
        actor=ActorType.AGENT,
        event_type="action_scheduled",
        inputs_digest="2" * 64,
        idempotency_key="idem_not_in_the_outbox",
        decision_rationale="an action nobody enqueued",
    )

    result = invariants.check_action_audit_pairing(conn)
    assert result.status is InvariantStatus.VIOLATED
    assert "idem_not_in_the_outbox" in result.detail


def test_no_external_actions_at_all_is_vacuous(conn):
    result = invariants.check_action_audit_pairing(conn)
    assert result.status is InvariantStatus.VACUOUS


# ---------------------------------------------------------------------------
# #9 -- every non-terminal case has one next step
# ---------------------------------------------------------------------------


def _walk_to(conn, case_id, *states, **field_updates):
    for state in states:
        case_machine.transition(
            conn, case_id, state, at=_STAMP, rationale="test batch", **field_updates
        )


def test_a_live_case_with_one_pending_action_holds(
    conn, make_obligation, make_case, make_debit_envelope
):
    _open_case(conn, make_obligation, make_case, 1)
    _enqueue_with_audit(
        conn,
        make_debit_envelope(case_id="case_1", action_id="act_1", obligation_id="obl_1"),
    )
    _walk_to(conn, "case_1", CaseState.PLANNED, CaseState.SCHEDULED)

    result = invariants.check_no_orphaned_live_case(conn)
    assert result.status is InvariantStatus.HOLDS
    assert result.candidates_examined == 1


def test_a_live_case_with_nothing_scheduled_is_orphaned(
    conn, make_obligation, make_case
):
    """§9.1: "No case can be silently orphaned." A case sitting in ``detected``
    with an empty outbox and no human queue is exactly that."""
    _open_case(conn, make_obligation, make_case, 1)

    result = invariants.check_no_orphaned_live_case(conn)
    assert result.status is InvariantStatus.VIOLATED
    assert result.offending_case_ids == ("case_1",)
    assert "orphaned" in result.detail


def test_a_live_case_with_two_pending_actions_is_caught(
    conn, make_obligation, make_case, make_debit_envelope
):
    """"Exactly one" is the invariant, and two queued money movements on one live
    case is the shape of a double debit that the Q1 index does not cover (the
    attempt sequences differ, so both rows are legal on their own)."""
    _open_case(conn, make_obligation, make_case, 1)
    for attempt in (1, 2):
        _enqueue_with_audit(
            conn,
            make_debit_envelope(
                case_id="case_1",
                action_id=f"act_{attempt}",
                obligation_id="obl_1",
                attempt_sequence=attempt,
            ),
        )
    _walk_to(conn, "case_1", CaseState.PLANNED, CaseState.SCHEDULED)

    result = invariants.check_no_orphaned_live_case(conn)
    assert result.status is InvariantStatus.VIOLATED
    assert result.offending_case_ids == ("case_1",)


def test_a_case_parked_for_a_human_is_not_evidence_that_nine_holds(
    conn, make_obligation, make_case
):
    """``awaiting_approval`` is accepted as "one open human task" -- and that
    acceptance is circular, because no human-task table exists: the state is both
    the claim and its only evidence. So a batch whose live cases are *all* proxy
    -satisfied is vacuous, not green."""
    _open_case(conn, make_obligation, make_case, 1)
    _walk_to(conn, "case_1", CaseState.PLANNED, CaseState.AWAITING_APPROVAL)

    result = invariants.check_no_orphaned_live_case(conn)
    assert result.status is InvariantStatus.VACUOUS
    assert "human" in result.detail


def _log_no_action(conn, case_id, event_type):
    """What ``flow`` writes when it decides, on the record, not to act on a case."""
    audit_store.append(
        conn,
        ts=_STAMP,
        case_id=case_id,
        actor=ActorType.SYSTEM,
        event_type=event_type,
        inputs_digest="0" * 64,
        decision_rationale="test batch",
    )


@pytest.mark.parametrize("event_type", sorted(invariants.NO_ACTION_DECISION_EVENTS))
def test_a_live_case_with_a_logged_no_action_decision_is_not_orphaned(
    conn, make_obligation, make_case, event_type
):
    """§9.1 forbids a case being *silently* orphaned, and the adverb is the rule.

    A0 is the no-action control by construction (§12.2) and the cut arms are not
    implemented, so both leave cases sitting in ``detected`` forever. That is not
    a next step -- but it is not silence either: the decision is a row in the
    hash-chained log, written at decision time by a different component into a
    different table. Unlike the ``awaiting_approval`` proxy below, which is its
    own only evidence, this evidence is independent of the state it excuses.
    """
    _open_case(conn, make_obligation, make_case, 1)
    _log_no_action(conn, "case_1", event_type)

    result = invariants.check_no_orphaned_live_case(conn)
    assert result.status is not InvariantStatus.VIOLATED
    assert result.offending_case_ids == ()


def test_a_logged_decision_explains_a_missing_next_step_it_does_not_supply_one(
    conn, make_obligation, make_case, make_debit_envelope
):
    """The decision-closed cases must not be counted among the satisfied ones.

    If they were, a batch of nothing but control cases would report ``HOLDS`` and
    the reader would conclude the agent had queued 36 next steps. The count is
    reported separately and the verdict still rests on the genuinely scheduled.
    """
    _open_case(conn, make_obligation, make_case, 1)
    _enqueue_with_audit(
        conn,
        make_debit_envelope(case_id="case_1", action_id="act_1", obligation_id="obl_1"),
    )
    _walk_to(conn, "case_1", CaseState.PLANNED, CaseState.SCHEDULED)
    _open_case(conn, make_obligation, make_case, 2)
    _log_no_action(conn, "case_2", "control_arm_no_action")

    result = invariants.check_no_orphaned_live_case(conn)
    assert result.status is InvariantStatus.HOLDS
    assert result.candidates_examined == 2
    assert "1 of 2" in result.detail
    assert "no-action decision" in result.detail


def test_an_unexplained_live_case_is_still_orphaned_beside_an_explained_one(
    conn, make_obligation, make_case
):
    """The exemption is not a blanket. A case with no queued action, no human
    owner and *no logged decision* is the bug this invariant exists to find, and
    it must still be named even when its neighbour is legitimately quiet."""
    _open_case(conn, make_obligation, make_case, 1)
    _log_no_action(conn, "case_1", "arm_not_routed")
    _open_case(conn, make_obligation, make_case, 2)

    result = invariants.check_no_orphaned_live_case(conn)
    assert result.status is InvariantStatus.VIOLATED
    assert result.offending_case_ids == ("case_2",)


def test_the_flow_writes_every_event_the_checker_accepts_as_a_decision(conn):
    """The join between the two modules, walked rather than spot-checked.

    ``NO_ACTION_DECISION_EVENTS`` is a set of string literals that ``flow`` must
    actually emit. Rename one there and this exemption silently stops applying --
    the checker would go back to reporting every control case as orphaned, which
    is at least loud. Rename one *here* and the exemption silently widens, which
    is not. So every member is required to appear in a real seeded run.
    """
    from reclaim import flow
    from reclaim.spine import seed
    from reclaim.spine.tables import audit_log

    cases = seed.generate(conn, n=60)
    flow.run(conn, cases)

    emitted = {
        row.event_type
        for row in conn.execute(sa.select(audit_log.c.event_type)).all()
    }
    assert invariants.NO_ACTION_DECISION_EVENTS <= emitted


def test_a_seeded_batch_leaves_no_case_unaccounted_for(conn):
    """End to end: after a full pass, invariant #9 is not violated.

    This is the check a judge sees in the demo report. It failed for 50 of 73
    live cases before the exemption above, every one of them a control or cut-arm
    case that the log had already explained.
    """
    from reclaim import flow
    from reclaim.sim import outcomes
    from reclaim.spine import seed

    cases = seed.generate(conn, n=200)
    flow.run(conn, cases)
    outcomes.resolve_batch(conn, cases)

    result = invariants.check_no_orphaned_live_case(conn)
    assert result.status is InvariantStatus.HOLDS, result.detail


def test_a_terminal_case_needs_no_next_step(conn, make_obligation, make_case):
    _open_case(conn, make_obligation, make_case, 1)
    case_machine.transition(
        conn,
        "case_1",
        CaseState.STOPPED,
        at=_STAMP,
        rationale="policy denied the proposed action",
        stop_reason=StopReason.POLICY_BLOCKED,
        stopped_at=_STAMP,
    )

    result = invariants.check_no_orphaned_live_case(conn)
    assert result.status is InvariantStatus.VACUOUS
    assert result.candidates_examined == 0


def test_the_human_task_proxy_states_are_live_states_a_human_owns():
    """Walks the table rather than spot-checking a member (CLAUDE.md).

    A terminal state in here would silently excuse a closed case from needing a
    next step -- harmless -- but a state nobody actually queues for a human would
    excuse a live one, which is the orphan #9 exists to find.
    """
    from reclaim.contracts.enums import ALLOWED_CASE_TRANSITIONS, TERMINAL_CASE_STATES

    assert invariants.HUMAN_TASK_PROXY_STATES
    for state in invariants.HUMAN_TASK_PROXY_STATES:
        assert state not in TERMINAL_CASE_STATES
        assert ALLOWED_CASE_TRANSITIONS[state]


# ---------------------------------------------------------------------------
# #6 -- total recovered per obligation <= amount owed
# ---------------------------------------------------------------------------

_TO_RECOVERED = (
    CaseState.PLANNED,
    CaseState.SCHEDULED,
    CaseState.EXECUTING,
    CaseState.RECOVERED,
)


def test_one_recovered_case_within_the_amount_owed_holds(
    conn, make_obligation, make_case
):
    _open_case(conn, make_obligation, make_case, 1)
    _walk_to(conn, "case_1", *_TO_RECOVERED)

    result = invariants.check_recovery_within_amount_owed(conn)
    assert result.status is InvariantStatus.HOLDS
    assert result.candidates_examined == 1


def test_two_recovered_cases_on_one_obligation_over_collect(
    conn, make_obligation, make_case
):
    """§13's anti-double-counting hazard, made concrete.

    The ledger allows a second case once the first is terminal, and each case
    recognises the *full* amount at risk. Two recovered cases on one Rs 1,499
    obligation therefore claim Rs 2,998 recovered against Rs 1,499 owed -- which no
    single-case check can see, because neither case is wrong on its own.
    """
    _open_case(conn, make_obligation, make_case, 1)
    _walk_to(conn, "case_1", *_TO_RECOVERED)

    reopened = make_case(
        case_id="case_2", obligation_id="obl_1", payer_id="payer_1"
    )
    ledger.open_case(conn, reopened)
    _walk_to(conn, "case_2", *_TO_RECOVERED)

    result = invariants.check_recovery_within_amount_owed(conn)
    assert result.status is InvariantStatus.VIOLATED
    assert set(result.offending_case_ids) == {"case_1", "case_2"}
    assert "obl_1" in result.detail


def test_an_over_collected_obligation_is_reported_not_raised(
    conn, make_obligation, make_case
):
    """The reason this module reads raw JSON instead of the models.

    ``Obligation`` refuses to *construct* a row whose collected amount exceeds its
    gross (its own validator cites invariant #6), so a validating read raises on
    exactly the row the checker exists to report -- and a checker that raises
    reports nothing. This test tampers with the stored JSON the way a bad migration
    or a direct write would, and asserts both halves: the ledger cannot read it,
    and the checker can.
    """
    from pydantic import ValidationError

    from reclaim.spine.tables import obligations as obligations_table

    _open_case(conn, make_obligation, make_case, 1)
    stored = json.loads(
        conn.execute(
            sa.select(obligations_table.c.data).where(
                obligations_table.c.obligation_id == "obl_1"
            )
        ).scalar_one()
    )
    stored["partial_payments"] = [
        {
            "amount": {"paise": 200000, "currency": "INR"},
            "received_at": _STAMP_TEXT,
            "match_method": "bank_feed",
            "reference": None,
        }
    ]
    conn.execute(
        obligations_table.update()
        .where(obligations_table.c.obligation_id == "obl_1")
        .values(data=json.dumps(stored))
    )

    with pytest.raises(ValidationError):
        ledger.get_obligation(conn, "obl_1")

    result = invariants.check_recovery_within_amount_owed(conn)
    assert result.status is InvariantStatus.VIOLATED
    assert "obl_1" in result.detail


def test_a_batch_with_no_recovery_yet_is_vacuous(conn, make_obligation, make_case):
    """Nothing has come back, so "recovered <= owed" is true of an empty sum. On a
    flow-only batch this is the honest verdict: the invariant needs the outcome
    half of the run before it says anything."""
    _open_case(conn, make_obligation, make_case, 1)

    result = invariants.check_recovery_within_amount_owed(conn)
    assert result.status is InvariantStatus.VACUOUS
    assert result.candidates_examined == 0
