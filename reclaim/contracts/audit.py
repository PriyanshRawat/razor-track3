"""The hash-chained append-only audit log (deliverable #5, part 2).

§15's row, verbatim::

    {ts, case_id, actor, event_type, inputs_digest, tool_call, tool_result_digest,
     policy_verdicts[], model_id, prompt_version, policy_version,
     decision_rationale, prev_hash, row_hash}

plus a ``sequence`` (see JC-27). This module is the demo's credibility: a reviewer
who does not trust the agent can still verify that what it says it did is what the
log says it did, and that the log has not been edited since.

CONTRACT DECISION (JC-27): rows are numbered, and the number is hashed
----------------------------------------------------------------------
§15 lists ``prev_hash`` but no sequence number. A pure ``prev_hash`` chain detects
*edits* but not *truncation*: lopping the last N rows off leaves a shorter chain
that still verifies perfectly. Adding a monotonic ``sequence`` inside the hashed
payload means a verifier can also state how long the chain claims to be, and a
deleted row in the middle shows up as a sequence gap even before the hash mismatch.
The cost is one integer per row.

CONTRACT DECISION (JC-28): the row hash is derived, and a claimed one is checked
--------------------------------------------------------------------------------
``row_hash`` is a ``computed_field``, exactly as ``ActionEnvelope.idempotency_key``
is: nothing in the system can write a row whose hash disagrees with its contents.
On the way *in* -- reading a stored row back for §15 replay -- a claimed hash is
accepted only if it equals the derived one. So an edited stored row does not
verify; it does not even parse. That is a stronger guarantee than "verify_chain
would have noticed", because it holds for code paths that never call
``verify_chain``.

The hashed payload deliberately **excludes** ``row_hash`` itself and **includes**
``prev_hash``: that is what makes it a chain rather than a set of independent
checksums.

CONTRACT DECISION (JC-29): digests, not payloads
------------------------------------------------
``inputs_digest`` and ``tool_result_digest`` are SHA-256 hex, not the inputs and
results themselves. Three reasons: PSP responses and customer messages contain
personal data that should not be duplicated into an append-only file; the log stays
small enough to verify in front of a judge; and the digest is enough to prove a
payload was not altered, given the payload. ``tool_call`` is an
``ActionEnvelope`` -- kept in full, because *what we did* is the thing under
review, and it carries no customer content beyond identifiers and template slots.

CONTRACT DECISION (JC-30): every verdict is recorded, allows included
--------------------------------------------------------------------
§14.1 requires it, and it is the difference between "the policy engine ran" and
"we believe the policy engine ran". ``policy_verdict_rule_ids`` is an ordered
tuple; ``policy_decision`` carries the composed decision when there was one.

CONTRACT DECISION (JC-31): ``event_type`` here is a free string, not an enum
---------------------------------------------------------------------------
The audit log records *our own* lifecycle transitions (``policy_evaluated``,
``approval_granted``, ``chain_sealed``), not the ingested-event vocabulary in
``events.EventType``. Freezing that list in Phase 0 would mean amending the
contract every time Phase 1 adds a log point, and a wrong-but-frozen enum is worse
than a validated string. It is constrained to a snake_case identifier so it stays
groupable, and ``AUDIT_EVENT_TYPES`` records the ones known today without closing
the set. **Flagged for review:** this is the one place in Phase 0 where a
vocabulary is open. See CONTRACTS.md.
"""

from __future__ import annotations

from typing import Any, Final, Iterable, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from reclaim.contracts.actions import ActionEnvelope
from reclaim.contracts.canonical import digest
from reclaim.contracts.enums import ActorType
from reclaim.contracts.ids import CaseId
from reclaim.contracts.policy_format import PolicyDecision
from reclaim.contracts.temporal import UtcDatetime
from reclaim.contracts.versions import AUDIT_SCHEMA_VERSION

__all__ = [
    "AUDIT_EVENT_TYPES",
    "AuditRow",
    "ChainVerification",
    "GENESIS_HASH",
    "append_row",
    "verify_chain",
]

#: What row 0 points at. All-zero rather than a random nonce so that a chain is
#: reproducible from its rows alone, with no out-of-band seed to lose.
GENESIS_HASH: Final[str] = "0" * 64

#: Log points known at Phase 0. Not exhaustive and not enforced (JC-31).
AUDIT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "case_opened",
        "case_state_changed",
        "diagnosis_produced",
        "plan_produced",
        "policy_evaluated",
        "approval_requested",
        "approval_granted",
        "approval_rejected",
        "action_scheduled",
        "action_executed",
        "action_failed",
        "action_suppressed",
        "message_delivered",
        "message_inbound",
        "payment_received",
        "reconciliation_run",
        "incident_opened",
        "incident_closed",
        "case_stopped",
        "case_recovered",
        "human_override",
        "chain_sealed",
    }
)

_HEX64 = r"^[0-9a-f]{64}$"
_SNAKE = r"^[a-z][a-z0-9_]{2,63}$"


class AuditRow(BaseModel):
    """One immutable, self-hashing log row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=0, description="Position in the chain (JC-27).")
    ts: UtcDatetime = Field(description="When the recorded thing happened (UTC).")
    case_id: CaseId | None = Field(
        default=None,
        description="None for chain-level or cohort-level rows (e.g. an incident "
        "opened across many cases).",
    )
    actor: ActorType
    event_type: str = Field(pattern=_SNAKE, description="See JC-31.")
    inputs_digest: str = Field(
        pattern=_HEX64,
        description="SHA-256 of the canonical form of everything the decision saw. "
        "Recomputable from the event store, which is how §15 replay works.",
    )
    tool_call: ActionEnvelope | None = Field(
        default=None,
        description="The action, in full, when this row records one. None for rows "
        "that record an observation rather than an act.",
    )
    tool_result_digest: str | None = Field(
        default=None,
        pattern=_HEX64,
        description="SHA-256 of the canonical form of the PSP/comms response.",
    )
    policy_verdict_rule_ids: tuple[str, ...] = Field(
        default=(),
        description="Every rule that returned a verdict, allows included (JC-30).",
    )
    policy_decision: PolicyDecision | None = Field(
        default=None,
        description="The composed decision, when this row records a policy "
        "evaluation. Carries the deciding rule and the effect.",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="Present on rows that executed something. Invariant #8: one "
        "external action, one audit row, one key.",
    )
    model_id: str | None = None
    prompt_version: str | None = None
    policy_version: str | None = None
    decision_rationale: str = Field(
        max_length=2000,
        description="Why, in one or two sentences a reviewer can read.",
    )
    prev_hash: str = Field(pattern=_HEX64)
    schema_version: str = AUDIT_SCHEMA_VERSION

    # -- hashing ----------------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def row_hash(self) -> str:
        """SHA-256 over the canonical form of every field except this one.

        ``prev_hash`` is inside the payload, which is what chains the rows.
        """
        return digest(self._hashed_payload())

    def _hashed_payload(self) -> dict[str, Any]:
        """Every field except ``row_hash``.

        ``row_hash`` is excluded at the serialiser level rather than popped after
        the fact: dumping it would re-enter this method and recurse.
        """
        return self.model_dump(mode="json", exclude={"row_hash"})

    @model_validator(mode="wrap")
    @classmethod
    def _claimed_hash_must_agree(cls, data: Any, handler: Any) -> "AuditRow":
        """A stored row whose contents were edited will not parse (JC-28)."""
        claimed: str | None = None
        if isinstance(data, dict) and "row_hash" in data:
            data = dict(data)
            claimed = data.pop("row_hash")
        row = handler(data)
        if claimed is not None and claimed != row.row_hash:
            raise ValueError(
                "row_hash does not match the row contents: this row was tampered "
                f"with, or written under a different canonical form. "
                f"claimed={claimed!r} derived={row.row_hash!r}"
            )
        return row

    # -- consistency ------------------------------------------------------

    @field_validator("policy_verdict_rule_ids")
    @classmethod
    def _verdict_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("policy_verdict_rule_ids contains duplicates")
        return value

    @model_validator(mode="after")
    def _an_executed_action_names_its_key(self) -> "AuditRow":
        """A row carrying a ``tool_call`` must carry that call's own key, so the
        log and the executor's dedupe table cannot disagree about what ran."""
        if self.tool_call is not None:
            if self.idempotency_key is None:
                raise ValueError(
                    "a row with a tool_call must carry its idempotency_key "
                    "(invariant #8)"
                )
            if self.idempotency_key != self.tool_call.idempotency_key:
                raise ValueError(
                    "idempotency_key disagrees with the tool_call it accompanies: "
                    f"{self.idempotency_key!r} vs {self.tool_call.idempotency_key!r}"
                )
            if self.case_id is not None and self.tool_call.case_id != self.case_id:
                raise ValueError(
                    "the tool_call belongs to a different case than the row"
                )
        return self

    @model_validator(mode="after")
    def _a_genesis_row_points_at_genesis(self) -> "AuditRow":
        if self.sequence == 0 and self.prev_hash != GENESIS_HASH:
            raise ValueError(
                "row 0 must have prev_hash = GENESIS_HASH; a first row pointing at "
                "something else means rows were dropped from the front"
            )
        return self

    @model_validator(mode="after")
    def _a_non_genesis_row_does_not_point_at_genesis(self) -> "AuditRow":
        if self.sequence > 0 and self.prev_hash == GENESIS_HASH:
            raise ValueError(
                f"row {self.sequence} points at GENESIS_HASH; only row 0 may"
            )
        return self


class ChainVerification(BaseModel):
    """The result of ``verify_chain``. A model rather than a bool so the CLI can
    print *where* a chain broke, which is the only useful thing to know."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_valid: bool
    rows_checked: int = Field(ge=0)
    first_bad_sequence: int | None = Field(
        default=None, description="Sequence number of the first row that failed."
    )
    reason: str = Field(default="", max_length=500)
    head_hash: str | None = Field(
        default=None,
        description="row_hash of the last row, when the chain is valid. Publishing "
        "this pins the whole log with 64 characters.",
    )


def verify_chain(rows: Sequence[AuditRow]) -> ChainVerification:
    """Recompute the chain and report the first place it breaks.

    Checks, in order, per row: the sequence increments by one; ``prev_hash``
    equals the predecessor's ``row_hash``; the row's own hash matches its
    contents. An empty log is valid -- vacuously, and it says so.

    Note that ``AuditRow`` already refuses to construct a row whose hash
    disagrees with its contents (JC-28), so the third check can only fire for
    rows built in memory. It is kept anyway: this function is the thing a
    reviewer runs, and it should not depend on the constructor's behaviour to be
    correct.
    """
    if not rows:
        return ChainVerification(
            is_valid=True, rows_checked=0, reason="empty chain: nothing to verify"
        )

    expected_prev = GENESIS_HASH
    expected_sequence = rows[0].sequence

    if expected_sequence != 0:
        return ChainVerification(
            is_valid=False,
            rows_checked=0,
            first_bad_sequence=rows[0].sequence,
            reason=f"chain starts at sequence {rows[0].sequence}, expected 0",
        )

    for index, row in enumerate(rows):
        if row.sequence != expected_sequence:
            return ChainVerification(
                is_valid=False,
                rows_checked=index,
                first_bad_sequence=row.sequence,
                reason=(
                    f"sequence gap: expected {expected_sequence}, found "
                    f"{row.sequence}; a row was deleted or reordered"
                ),
            )
        if row.prev_hash != expected_prev:
            return ChainVerification(
                is_valid=False,
                rows_checked=index,
                first_bad_sequence=row.sequence,
                reason=(
                    f"prev_hash mismatch at sequence {row.sequence}: row points at "
                    f"{row.prev_hash[:12]}..., predecessor hashes to "
                    f"{expected_prev[:12]}..."
                ),
            )
        recomputed = digest(row._hashed_payload())
        if recomputed != row.row_hash:
            return ChainVerification(
                is_valid=False,
                rows_checked=index,
                first_bad_sequence=row.sequence,
                reason=f"row_hash mismatch at sequence {row.sequence}",
            )
        expected_prev = row.row_hash
        expected_sequence += 1

    return ChainVerification(
        is_valid=True,
        rows_checked=len(rows),
        reason="chain verified",
        head_hash=rows[-1].row_hash,
    )


def append_row(rows: Sequence[AuditRow], **fields: Any) -> AuditRow:
    """Build the next row, linked to the tail of ``rows``.

    The only intended way to write a row: ``sequence`` and ``prev_hash`` are
    derived from the existing chain rather than passed in, so an off-by-one is not
    expressible at the call site.
    """
    if "prev_hash" in fields or "sequence" in fields:
        raise ValueError(
            "append_row derives sequence and prev_hash from the chain; passing "
            "either by hand defeats the point"
        )
    tail = rows[-1] if rows else None
    return AuditRow(
        sequence=0 if tail is None else tail.sequence + 1,
        prev_hash=GENESIS_HASH if tail is None else tail.row_hash,
        **fields,
    )
