"""Phase 1 spine: the ``verify_chain`` command (§15, demoed at §17's 5:40 beat).

Every test here damages a *real* database with SQL -- ``UPDATE audit_log SET data``,
``DELETE FROM audit_log`` -- rather than mocking a verdict. A tamper-evidence tool
tested against a stub proves only that the stub was returned; the thing under test is
whether real damage survives a real read.

The two tests that matter most are the ones about what this tool *cannot* do:

* ``test_a_deleted_tail_row_is_not_detected_without_a_pinned_head`` pins the known
  hole. Truncation at the tail leaves a shorter chain that verifies perfectly, and
  nothing inside the table can say otherwise. If someone later makes that detectable,
  this test fails and the docstring that admits the hole gets corrected with it.
* ``test_a_forged_link_is_reported_as_a_broken_link`` builds a row whose stored hash
  *agrees* with its edited contents -- the tamperer who recomputes. It gets past
  ``AuditRow``'s parse-time check and is caught only by the chain walk, which is why
  this module must call the frozen ``verify_chain`` and not a local approximation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from reclaim.contracts.audit import AuditRow
from reclaim.contracts.canonical import digest
from reclaim.contracts.enums import ActorType
from reclaim.spine import audit_store, verify
from reclaim.spine.db import create_all, get_engine
from reclaim.spine.tables import audit_log

TS = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)
DIGEST = digest({"observed": "case_opened"})

#: A syntactically valid 64-hex hash that is not any row's hash. Used to forge a
#: ``prev_hash`` that still satisfies ``AuditRow``'s pattern and its
#: "only row 0 may point at genesis" validator.
FORGED_HASH = digest({"forged": "not a real predecessor"})

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------- helpers


def _append(conn, **over):
    fields = dict(
        ts=TS,
        actor=ActorType.SYSTEM,
        event_type="case_opened",
        inputs_digest=DIGEST,
        decision_rationale="detector opened the case",
    )
    fields.update(over)
    return audit_store.append(conn, **fields)


def _three_rows(conn):
    _append(conn)
    _append(conn, event_type="case_state_changed")
    _append(conn, event_type="case_recovered")


def _stored_data(conn, sequence: int) -> dict:
    raw = conn.execute(
        sa.select(audit_log.c.data).where(audit_log.c.sequence == sequence)
    ).scalar_one()
    return json.loads(raw)


def _overwrite_data(conn, sequence: int, payload: dict) -> None:
    conn.execute(
        audit_log.update()
        .where(audit_log.c.sequence == sequence)
        .values(data=json.dumps(payload))
    )


def _build_file_chain(path: Path, *, rows: int) -> str:
    """Create a file-backed SQLite database holding ``rows`` audit rows.

    Returns the URL. The engine is disposed before returning so the subprocess
    that opens the file next is not racing an open handle on Windows.
    """
    url = f"sqlite:///{path.as_posix()}"
    engine = get_engine(url)
    try:
        create_all(engine)
        with engine.begin() as conn:
            for index in range(rows):
                event = "case_opened" if index == 0 else "case_stopped"
                _append(conn, event_type=event)
    finally:
        engine.dispose()
    return url


def _run_cli(url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(
        os.environ, RECLAIM_DATABASE_URL=url, PYTHONIOENCODING="utf-8"
    )
    return subprocess.run(
        [sys.executable, "-m", "reclaim.spine.verify", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO_ROOT),
    )


# ------------------------------------------------------- the clean chain


def test_a_clean_chain_verifies(conn):
    _three_rows(conn)
    report = verify.verify_database_chain(conn)
    assert report.verified is True
    assert report.kind is verify.ChainFailureKind.NONE
    assert report.rows_checked == 3
    assert report.first_bad_sequence is None


def test_a_clean_chain_reports_the_tail_row_hash_as_its_head(conn):
    _three_rows(conn)
    # Derived from the frozen contract's own computed field, not from verify.py.
    expected_head = audit_store.tail(conn).row_hash
    assert verify.verify_database_chain(conn).head_hash == expected_head


def test_a_clean_chain_verifies_against_a_correctly_pinned_head(conn):
    _three_rows(conn)
    head = audit_store.tail(conn).row_hash
    report = verify.verify_database_chain(conn, expected_head_hash=head)
    assert report.verified is True
    assert report.kind is verify.ChainFailureKind.NONE


# ------------------------------------------------------- the empty chain


def test_an_empty_chain_is_not_reported_as_verified(conn):
    # The trap this closes: a wiped audit table must not print a green tick.
    report = verify.verify_database_chain(conn)
    assert report.verified is False
    assert report.kind is verify.ChainFailureKind.EMPTY_CHAIN
    assert report.rows_checked == 0


def test_an_empty_chain_still_records_the_contracts_vacuous_verdict(conn):
    # The frozen verify_chain calls an empty log valid. We deliberately disagree at
    # this layer; both answers are kept so the divergence is visible, not silent.
    report = verify.verify_database_chain(conn)
    assert report.contract_is_valid is True


# ----------------------------------------------------------- real damage


def test_an_edited_row_is_reported_as_tampered_rather_than_raising(conn):
    _three_rows(conn)
    payload = _stored_data(conn, 1)
    payload["decision_rationale"] = "nothing to see here"
    _overwrite_data(conn, 1, payload)

    report = verify.verify_database_chain(conn)
    assert report.verified is False
    assert report.kind is verify.ChainFailureKind.ROW_TAMPERED
    assert report.first_bad_sequence == 1


def test_an_edited_row_does_not_leak_a_pydantic_error_into_the_reason(conn):
    _three_rows(conn)
    payload = _stored_data(conn, 0)
    payload["event_type"] = "case_recovered"
    _overwrite_data(conn, 0, payload)

    reason = verify.verify_database_chain(conn).reason
    assert "ValidationError" not in reason
    assert "Traceback" not in reason
    assert "sequence 0" in reason


def test_a_structurally_broken_row_is_reported_as_unreadable(conn):
    _three_rows(conn)
    conn.execute(
        audit_log.update()
        .where(audit_log.c.sequence == 2)
        .values(data="{ this is not json")
    )
    report = verify.verify_database_chain(conn)
    assert report.verified is False
    assert report.kind is verify.ChainFailureKind.ROW_UNREADABLE
    assert report.first_bad_sequence == 2


def test_a_forged_link_is_reported_as_a_broken_link(conn):
    """The tamperer who recomputes the row hash after editing.

    ``AuditRow`` accepts this row -- its claimed hash agrees with its contents --
    so JC-28 does not fire. Only walking the chain catches it.
    """
    _three_rows(conn)
    original = _stored_data(conn, 1)
    original.pop("row_hash")
    original["prev_hash"] = FORGED_HASH
    forged = AuditRow.model_validate(original)
    _overwrite_data(conn, 1, json.loads(forged.model_dump_json()))

    report = verify.verify_database_chain(conn)
    assert report.verified is False
    assert report.kind is verify.ChainFailureKind.BROKEN_LINK
    assert report.first_bad_sequence == 1


def test_a_deleted_middle_row_is_reported_as_a_sequence_gap(conn):
    _three_rows(conn)
    conn.execute(audit_log.delete().where(audit_log.c.sequence == 1))

    report = verify.verify_database_chain(conn)
    assert report.verified is False
    assert report.kind is verify.ChainFailureKind.SEQUENCE_GAP
    assert report.first_bad_sequence == 2


def test_a_deleted_tail_row_is_not_detected_without_a_pinned_head(conn):
    """The hole, asserted rather than hidden.

    Nothing is stored outside ``audit_log``, so a chain with its last row removed is
    indistinguishable from a chain that was never that long. If this test ever fails,
    truncation became detectable and every docstring admitting otherwise is now wrong.
    """
    _three_rows(conn)
    conn.execute(audit_log.delete().where(audit_log.c.sequence == 2))

    report = verify.verify_database_chain(conn)
    assert report.verified is True
    assert report.rows_checked == 2


def test_a_deleted_tail_row_is_detected_when_the_head_was_pinned(conn):
    _three_rows(conn)
    head_before = audit_store.tail(conn).row_hash
    conn.execute(audit_log.delete().where(audit_log.c.sequence == 2))

    report = verify.verify_database_chain(conn, expected_head_hash=head_before)
    assert report.verified is False
    assert report.kind is verify.ChainFailureKind.HEAD_MISMATCH


def test_an_edited_mirror_column_is_detected_even_though_it_is_not_hashed(conn):
    # `data` is the source of truth; sequence/prev_hash/row_hash are duplicated into
    # columns so a reviewer can read the log with plain SQL. Editing only the column
    # leaves the hash chain intact, so only a mirror check can see it.
    _three_rows(conn)
    conn.execute(
        audit_log.update()
        .where(audit_log.c.sequence == 1)
        .values(row_hash=FORGED_HASH)
    )
    report = verify.verify_database_chain(conn)
    assert report.verified is False
    assert report.kind is verify.ChainFailureKind.COLUMN_MISMATCH
    assert report.first_bad_sequence == 1


def test_a_missing_audit_table_is_reported_not_treated_as_an_empty_log(conn):
    # Dropping the table is the crudest tamper there is. It must never be the same
    # answer as "this log has no rows yet", and it must never be exit 0.
    engine = get_engine("sqlite://")
    try:
        with engine.begin() as bare:
            report = verify.verify_database_chain(bare)
    finally:
        engine.dispose()
    assert report.verified is False
    assert report.kind is verify.ChainFailureKind.CHAIN_UNREADABLE


# ---------------------------------------------------- the report's own guard


def test_a_report_cannot_claim_verified_while_naming_a_failure_kind():
    with pytest.raises(ValidationError):
        verify.ChainReport(
            verified=True,
            contract_is_valid=False,
            kind=verify.ChainFailureKind.BROKEN_LINK,
            rows_checked=1,
            reason="inconsistent",
        )


# ------------------------------------------------------------- rendering


def test_the_rendered_report_names_the_head_hash_when_the_chain_verifies(conn):
    _three_rows(conn)
    report = verify.verify_database_chain(conn)
    text = verify.render_report(report, database_url="sqlite://")
    assert "VERIFIED" in text
    assert report.head_hash in text


def test_the_rendered_report_warns_that_an_unpinned_tail_is_unproven(conn):
    _three_rows(conn)
    text = verify.render_report(
        verify.verify_database_chain(conn), database_url="sqlite://"
    )
    assert "--expect-head" in text


def test_the_rendered_report_names_the_bad_sequence_when_the_chain_breaks(conn):
    _three_rows(conn)
    conn.execute(audit_log.delete().where(audit_log.c.sequence == 1))
    text = verify.render_report(
        verify.verify_database_chain(conn), database_url="sqlite://"
    )
    assert "NOT VERIFIED" in text
    assert "sequence 2" in text


def test_the_rendered_report_is_pure_ascii(conn):
    # The demo machine's console is cp1252 (CONTRACTS.md Q9). This command is the
    # §17 closing beat; it must not be the thing that raises UnicodeEncodeError.
    _three_rows(conn)
    text = verify.render_report(
        verify.verify_database_chain(conn), database_url="sqlite://"
    )
    text.encode("cp1252")


# ------------------------------------------------------------ exit codes


def test_the_cli_exits_zero_on_a_clean_chain(tmp_path):
    url = _build_file_chain(tmp_path / "clean.db", rows=3)
    done = _run_cli(url)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "VERIFIED" in done.stdout


def test_the_cli_exits_non_zero_on_a_tampered_chain(tmp_path):
    path = tmp_path / "tampered.db"
    url = _build_file_chain(path, rows=3)
    engine = get_engine(url)
    try:
        with engine.begin() as conn:
            payload = _stored_data(conn, 1)
            payload["decision_rationale"] = "nothing to see here"
            _overwrite_data(conn, 1, payload)
    finally:
        engine.dispose()

    done = _run_cli(url)
    assert done.returncode == verify.EXIT_FAILED
    assert done.returncode != 0
    assert "NOT VERIFIED" in done.stdout


def test_the_cli_exits_non_zero_on_an_empty_chain(tmp_path):
    url = _build_file_chain(tmp_path / "empty.db", rows=0)
    done = _run_cli(url)
    assert done.returncode == verify.EXIT_EMPTY
    assert done.returncode != 0


def test_the_cli_exits_non_zero_when_the_pinned_head_does_not_match(tmp_path):
    url = _build_file_chain(tmp_path / "pinned.db", rows=3)
    done = _run_cli(url, "--expect-head", FORGED_HASH)
    assert done.returncode == verify.EXIT_FAILED
    assert "NOT VERIFIED" in done.stdout
