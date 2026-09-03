"""``verify_chain`` for the stored audit log (§15; the §17 5:40 closing beat).

§15 promises "a ``verify_chain`` command re-computes the chain live". This is that
command, in two layers: ``verify_database_chain`` returns a frozen verdict a caller
can act on, and ``python -m reclaim.spine.verify`` prints it and exits non-zero when
the chain does not verify. A verification tool that exits 0 on failure is not a
verification tool; the exit code is the part CI reads.

**The verdict is the contract's, not ours.** This module calls the frozen
``reclaim.contracts.audit.verify_chain`` and never recomputes a hash. If the CLI
could disagree with the contract about whether a chain verifies, the disagreement
itself would be the story -- a reviewer would have two answers and no way to choose.
Everything here is therefore either *reading* (getting rows out of the database
without crashing on a damaged one), *classifying* (saying what kind of damage it is),
or *rendering*. The yes/no comes from one place.

Classification without re-deriving hashes
-----------------------------------------
``ChainFailureKind`` is decided by elimination, not by parsing the contract's prose:

* the read failed             -> ``ROW_TAMPERED`` / ``ROW_UNREADABLE`` /
                                 ``CHAIN_UNREADABLE``
* there are no rows           -> ``EMPTY_CHAIN``
* ``verify_chain`` said valid -> ``NONE``
* sequences are not 0..n-1    -> ``SEQUENCE_GAP`` (an integer check, not a hash check)
* otherwise                   -> ``BROKEN_LINK``

The cost of matching on the contract's ``reason`` string instead would be a silent
mislabel the day someone rewords a message. The cost of *this* is that a future
failure mode added to ``verify_chain`` lands in ``BROKEN_LINK`` -- wrong label, right
verdict, non-zero exit. Failing safe is the whole point.

What this command can and cannot prove
--------------------------------------
It proves that **no row was edited and no row was removed from the middle or the
front**. It does **not** prove that no rows were removed from the *end*.

That is not an oversight, it is arithmetic. Nothing is stored outside ``audit_log``,
so a chain with its last N rows deleted is byte-for-byte a chain that was never that
long: sequences still run 0..k, every ``prev_hash`` still matches, every row still
hashes to its contents. JC-27's sequence number makes a *gap* visible; it cannot make
a missing tail visible, because there is no k+1 row left to contradict.

Closing that requires one fact held **outside** the table. Two ways, both cheap:

1. ``--expect-head <hash>``: pin the head hash somewhere the tamperer does not own
   (the pre-registration commit, the scoreboard, a judge's notebook -- 64 characters).
   Because JC-27 hashes the sequence number, the head hash commits to the chain's
   *length* as well as its contents, so a pinned head detects truncation exactly.
   ``HEAD_MISMATCH`` is that check, and ``test_a_deleted_tail_row_is_detected_when_
   the_head_was_pinned`` is the proof.
2. Periodically append a ``chain_sealed`` row (already in ``AUDIT_EVENT_TYPES``) whose
   payload commits to the head, and publish it. This only moves the boundary: a
   truncation that also removes the last seal is invisible again unless the seal was
   published outside the table. The seal is a convenience; the publication is the
   security.

Without one of those, an unpinned "VERIFIED" means *the rows that are here are
consistent* -- and the rendered report says exactly that rather than implying more.

Two further limits, stated because a compliance reviewer will find them anyway:

* A tamperer with write access who edits a row **and recomputes every hash after it**
  produces a chain that verifies. This is why ``BROKEN_LINK`` exists as a distinct
  kind -- it is what the *careless* forger leaves behind -- and why the pinned head
  is the only defence against the careful one.
* An empty log is reported as ``EMPTY_CHAIN`` and exits ``EXIT_EMPTY``, **not** as
  verified, even though the frozen ``verify_chain`` calls an empty log vacuously
  valid. A wiped audit table returning a green tick is precisely how a wipe would
  pass review. Both answers are carried on the report (``verified`` and
  ``contract_is_valid``) so the divergence is visible rather than a silent override.

Output is deliberately pure ASCII: this runs on a cp1252 console (CONTRACTS.md Q9),
and the closing beat of the demo must not be the thing that raises
``UnicodeEncodeError``.
"""

from __future__ import annotations

import argparse
import json
import os
from enum import StrEnum
from typing import Any, Sequence

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from reclaim.contracts.audit import AuditRow, verify_chain
from reclaim.spine import audit_store, db
from reclaim.spine.tables import audit_log

__all__ = [
    "EXIT_EMPTY",
    "EXIT_FAILED",
    "EXIT_OK",
    "ChainFailureKind",
    "ChainReport",
    "exit_code",
    "main",
    "render_report",
    "verify_database_chain",
]

#: The chain verified and had at least one row.
EXIT_OK = 0
#: The chain did not verify. Any damage, and a missing table, lands here.
EXIT_FAILED = 1
#: There was nothing to verify. Distinct from OK so that `verify && deploy` cannot be
#: satisfied by an emptied log, and distinct from FAILED so that a caller who really
#: does mean "a fresh database is fine" can say so.
EXIT_EMPTY = 2


class ChainFailureKind(StrEnum):
    """What kind of damage, not just that there was some.

    A reviewer's next question after "it failed" is always "failed how?" -- an edited
    row, a deleted one and a dropped table call for three different responses.
    """

    NONE = "none"
    EMPTY_CHAIN = "empty_chain"
    CHAIN_UNREADABLE = "chain_unreadable"
    ROW_UNREADABLE = "row_unreadable"
    ROW_TAMPERED = "row_tampered"
    SEQUENCE_GAP = "sequence_gap"
    BROKEN_LINK = "broken_link"
    COLUMN_MISMATCH = "column_mismatch"
    HEAD_MISMATCH = "head_mismatch"


class ChainReport(BaseModel):
    """The verdict, frozen, with enough detail to act on.

    ``verified`` and ``contract_is_valid`` are both kept: the first is this layer's
    stricter answer (an empty log is not verified), the second is
    ``ChainVerification.is_valid`` verbatim. Collapsing them would hide the one place
    this module deliberately disagrees with the contract it wraps.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verified: bool
    contract_is_valid: bool
    kind: ChainFailureKind
    rows_checked: int = Field(ge=0)
    reason: str = Field(default="", max_length=500)
    head_hash: str | None = Field(
        default=None,
        description="row_hash of the last row when the chain verifies. Publishing "
        "this pins the whole log -- length included (JC-27) -- with 64 characters.",
    )
    first_bad_sequence: int | None = None
    expected_head_hash: str | None = Field(
        default=None, description="The head the caller pinned, if any."
    )

    @model_validator(mode="after")
    def _verified_means_no_failure_kind(self) -> "ChainReport":
        """``verified`` is exactly ``kind is NONE``.

        Two fields that must agree will eventually not. Asserting it here means a
        future branch that sets one without the other fails at construction, in the
        tool's own process, rather than printing a green tick over a named failure.
        """
        if self.verified != (self.kind is ChainFailureKind.NONE):
            raise ValueError(
                f"verified={self.verified} disagrees with kind={self.kind.value!r}; "
                "a verified chain has kind 'none' and nothing else does"
            )
        return self


def _failed(
    kind: ChainFailureKind,
    reason: str,
    *,
    rows_checked: int = 0,
    first_bad_sequence: int | None = None,
    contract_is_valid: bool = False,
    expected_head_hash: str | None = None,
) -> ChainReport:
    return ChainReport(
        verified=False,
        contract_is_valid=contract_is_valid,
        kind=kind,
        rows_checked=rows_checked,
        reason=reason[:500],
        first_bad_sequence=first_bad_sequence,
        expected_head_hash=expected_head_hash,
    )


def _attribute_read_failure(conn: Connection) -> ChainReport:
    """Find which stored row will not parse, and say why.

    ``audit_store.read_all`` validates every row and raises on the first bad one
    without naming it -- correct for the application, useless for a reviewer. This
    re-reads row by row purely to attribute blame, and keys off the ``sequence``
    *column* rather than the blob, because the blob's own sequence may be the thing
    that was edited.

    The tampered/unreadable split is made by re-parsing the row with ``row_hash``
    removed. If it then validates, the row is well-formed and only its claimed hash
    disagreed -- i.e. the contents were edited after writing (JC-28). No string
    matching on pydantic's message, which would drift.
    """
    parsed = 0
    stored = conn.execute(
        sa.select(audit_log.c.sequence, audit_log.c.data).order_by(
            audit_log.c.sequence.asc()
        )
    ).all()

    for sequence, blob in stored:
        try:
            payload = json.loads(blob)
        except (TypeError, ValueError):
            return _failed(
                ChainFailureKind.ROW_UNREADABLE,
                f"the row stored at sequence {sequence} is not valid JSON",
                rows_checked=parsed,
                first_bad_sequence=sequence,
            )
        if not isinstance(payload, dict):
            return _failed(
                ChainFailureKind.ROW_UNREADABLE,
                f"the row stored at sequence {sequence} is not a JSON object",
                rows_checked=parsed,
                first_bad_sequence=sequence,
            )
        try:
            AuditRow.model_validate(payload)
        except (TypeError, ValueError):
            without_claim = {k: v for k, v in payload.items() if k != "row_hash"}
            try:
                AuditRow.model_validate(without_claim)
            except (TypeError, ValueError):
                return _failed(
                    ChainFailureKind.ROW_UNREADABLE,
                    f"the row stored at sequence {sequence} is not a valid audit "
                    "row: fields are missing, malformed, or mutually inconsistent",
                    rows_checked=parsed,
                    first_bad_sequence=sequence,
                )
            return _failed(
                ChainFailureKind.ROW_TAMPERED,
                f"the row stored at sequence {sequence} no longer hashes to its "
                "recorded row_hash: its contents were edited after it was written",
                rows_checked=parsed,
                first_bad_sequence=sequence,
            )
        parsed += 1

    # Every row parses on the second pass. Only reachable if the log changed between
    # the two reads; reported rather than swallowed, because a log that answers
    # differently twice in a row is itself the finding.
    return _failed(
        ChainFailureKind.CHAIN_UNREADABLE,
        "the log failed to read but every row parses on re-read: it is being "
        "written to concurrently, or storage is returning inconsistent results",
        rows_checked=parsed,
    )


def _first_column_divergence(
    conn: Connection, rows: Sequence[AuditRow]
) -> int | None:
    """The first sequence whose mirror columns disagree with the row they mirror.

    ``audit_log`` duplicates ``sequence``, ``prev_hash`` and ``row_hash`` out of the
    JSON blob so a reviewer can read the log with plain SQL. Those columns are *not*
    hashed, so editing one leaves the chain intact -- an ``UPDATE`` against the column
    a human reads would otherwise be invisible. This is the only check here that the
    hash chain cannot make for us.
    """
    mirrors = conn.execute(
        sa.select(
            audit_log.c.sequence, audit_log.c.prev_hash, audit_log.c.row_hash
        ).order_by(audit_log.c.sequence.asc())
    ).all()
    for (sequence, prev_hash, row_hash), row in zip(mirrors, rows):
        if (sequence, prev_hash, row_hash) != (
            row.sequence,
            row.prev_hash,
            row.row_hash,
        ):
            return sequence
    return None


def verify_database_chain(
    conn: Connection, *, expected_head_hash: str | None = None
) -> ChainReport:
    """Re-compute the stored audit chain and report the verdict. Never raises.

    ``expected_head_hash`` is the out-of-band pin described in the module docstring.
    Supply it and tail truncation becomes detectable; omit it and it is not -- see
    ``HEAD_MISMATCH`` and the module docstring for why that is arithmetic rather than
    a missing feature.

    Not raising is a requirement, not politeness: the caller of a tamper-evidence
    tool is asking *whether* the data is damaged, so damaged data must produce an
    answer. A pydantic traceback is not an answer.
    """
    try:
        rows = audit_store.read_all(conn)
    except SQLAlchemyError as exc:
        # The table is missing, or unreadable. A dropped audit_log is the crudest
        # tamper there is; it must never share an answer with "no rows yet".
        return _failed(
            ChainFailureKind.CHAIN_UNREADABLE,
            f"the audit log could not be read: {type(exc).__name__}",
            expected_head_hash=expected_head_hash,
        )
    except (TypeError, ValueError):
        report = _attribute_read_failure(conn)
        return report.model_copy(update={"expected_head_hash": expected_head_hash})

    if not rows:
        return _failed(
            ChainFailureKind.EMPTY_CHAIN,
            "the audit log has no rows: there is nothing to verify, which is not "
            "the same as verified",
            contract_is_valid=True,
            expected_head_hash=expected_head_hash,
        )

    verdict = verify_chain(rows)
    if not verdict.is_valid:
        contiguous = [row.sequence for row in rows] == list(range(len(rows)))
        return _failed(
            ChainFailureKind.BROKEN_LINK
            if contiguous
            else ChainFailureKind.SEQUENCE_GAP,
            verdict.reason,
            rows_checked=verdict.rows_checked,
            first_bad_sequence=verdict.first_bad_sequence,
            expected_head_hash=expected_head_hash,
        )

    if expected_head_hash is not None and verdict.head_hash != expected_head_hash:
        return _failed(
            ChainFailureKind.HEAD_MISMATCH,
            f"the chain is internally consistent but its head is "
            f"{verdict.head_hash} and the pinned head is {expected_head_hash}: rows "
            "were removed from the end, or this is not the log that was pinned",
            rows_checked=verdict.rows_checked,
            first_bad_sequence=rows[-1].sequence,
            expected_head_hash=expected_head_hash,
        )

    diverged = _first_column_divergence(conn, rows)
    if diverged is not None:
        return _failed(
            ChainFailureKind.COLUMN_MISMATCH,
            f"the hash chain verifies, but the queryable columns at sequence "
            f"{diverged} disagree with the row they mirror: someone edited the log "
            "a reviewer reads with SQL without touching the log that is hashed",
            rows_checked=verdict.rows_checked,
            first_bad_sequence=diverged,
            expected_head_hash=expected_head_hash,
        )

    return ChainReport(
        verified=True,
        contract_is_valid=True,
        kind=ChainFailureKind.NONE,
        rows_checked=verdict.rows_checked,
        reason=verdict.reason,
        head_hash=verdict.head_hash,
        expected_head_hash=expected_head_hash,
    )


def exit_code(report: ChainReport) -> int:
    """The process exit code for a report. See the ``EXIT_*`` constants."""
    if report.kind is ChainFailureKind.NONE:
        return EXIT_OK
    if report.kind is ChainFailureKind.EMPTY_CHAIN:
        return EXIT_EMPTY
    return EXIT_FAILED


_RULE = "=" * 72


def _wrap(text: str, indent: str, width: int = 72) -> list[str]:
    lines: list[str] = []
    current = indent
    for word in text.split():
        candidate = word if current == indent else f"{current} {word}"
        if current != indent and len(candidate) > width:
            lines.append(current)
            current = f"{indent}{word}"
        else:
            current = candidate if current != indent else f"{indent}{word}"
    lines.append(current)
    return lines


def render_report(report: ChainReport, *, database_url: str) -> str:
    """The report a judge reads over someone's shoulder.

    Pure ASCII on purpose (CONTRACTS.md Q9). The headline is the first thing on the
    page because that is the only line most readers will read; the caveat about the
    unpinned tail is printed on *success*, since that is the one moment a reader is
    inclined to over-read the result.
    """
    lines = [
        "RECLAIM -- audit chain verification (HACKATHON_PLAN.md section 15)",
        _RULE,
        f"  database    {database_url}",
        f"  rows read   {report.rows_checked}",
    ]
    if report.head_hash is not None:
        lines.append(f"  head hash   {report.head_hash}")
    if report.expected_head_hash is not None:
        lines.append(f"  pinned head {report.expected_head_hash}")
    if report.first_bad_sequence is not None:
        lines.append(f"  first fault sequence {report.first_bad_sequence}")
    if not report.verified:
        # Only on failure: "fault: none" on a green run is noise, and the headline
        # below already says it.
        lines.append(f"  fault       {report.kind.value}")
    lines.append("")

    if report.verified:
        lines.append("  VERIFIED -- every row re-hashed from its own contents, every")
        lines.append("              link matched its predecessor, no sequence gaps.")
        lines.append("")
        if report.expected_head_hash is None:
            lines.extend(
                _wrap(
                    "NOTE This proves no row was edited and none was removed from "
                    "the front or the middle. It cannot prove none were removed "
                    "from the END: a truncated chain is byte-for-byte a chain that "
                    "was never longer. Re-run with --expect-head "
                    + (report.head_hash or "<hash>")
                    + " against a head recorded outside this database to pin that "
                    "too.",
                    indent="  ",
                )
            )
        else:
            lines.append("  The head matches the pinned value, so the chain is also")
            lines.append("  provably untruncated.")
    else:
        lines.append("  NOT VERIFIED")
        lines.extend(_wrap(report.reason, indent="  "))

    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m reclaim.spine.verify``. Returns the process exit code.

    Exposed as a module entry point rather than a ``[project.scripts]`` console
    script so that it works from a source checkout with nothing installed -- which is
    the state a judge's machine is in.
    """
    parser = argparse.ArgumentParser(
        prog="python -m reclaim.spine.verify",
        description=(
            "Re-compute the hash chain of the stored audit log and report whether "
            "it verifies. Exits 0 when it verifies, 1 when it does not, and 2 when "
            "there is nothing to verify."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=f"SQLAlchemy URL. Defaults to ${db.ENV_VAR}, then {db.DEFAULT_URL!r}.",
    )
    parser.add_argument(
        "--expect-head",
        default=None,
        metavar="HASH",
        help=(
            "The head row_hash recorded outside this database. Supplying it is what "
            "makes truncation at the tail detectable."
        ),
    )
    args = parser.parse_args(argv)

    url = args.database_url or os.environ.get(db.ENV_VAR) or db.DEFAULT_URL
    report = _report_for_url(url, expected_head_hash=args.expect_head)
    print(render_report(report, database_url=url))
    return exit_code(report)


def _report_for_url(url: str, *, expected_head_hash: str | None) -> ChainReport:
    engine: Any = None
    try:
        engine = db.get_engine(url)
        with engine.connect() as conn:
            return verify_database_chain(
                conn, expected_head_hash=expected_head_hash
            )
    except SQLAlchemyError as exc:
        # A bad URL or an unreachable database is a failure to verify, not a crash:
        # the CLI's contract is "non-zero unless the chain verified".
        return _failed(
            ChainFailureKind.CHAIN_UNREADABLE,
            f"could not open {url}: {type(exc).__name__}",
            expected_head_hash=expected_head_hash,
        )
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())
