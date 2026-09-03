"""Identifier types.

All identifiers are opaque strings with a mandatory typed prefix. The prefix is
not decoration: it makes an ID confusion bug (passing a ``payer_id`` where an
``obligation_id`` is expected) a validation error rather than a silent wrong
lookup, and it makes audit rows readable without a join.

CONTRACT DECISION (JC-03): IDs are prefixed strings rather than UUID objects so
that they survive JSON round-trips, sort readably in the ledger UI, and hash
canonically in the audit chain without a serialiser hook.
"""

from __future__ import annotations

import re
from typing import Annotated, Final

from pydantic import StringConstraints

__all__ = [
    "ActionId",
    "AttemptId",
    "CaseId",
    "CohortId",
    "DiagnosisId",
    "DocumentId",
    "EvidenceId",
    "HoldId",
    "IncidentId",
    "LinkId",
    "MandateId",
    "MessageId",
    "NotificationId",
    "ObligationId",
    "PayerId",
    "PlanId",
    "PromiseId",
    "SubscriptionId",
    "ToolResultId",
    "ID_PREFIXES",
    "id_prefix_of",
    "is_valid_id",
]

_BODY = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"


def _typed_id(prefix: str) -> object:
    return Annotated[str, StringConstraints(pattern=rf"^{prefix}_{_BODY}$", strip_whitespace=True)]


CaseId = _typed_id("case")
ObligationId = _typed_id("obl")
PayerId = _typed_id("payer")
MandateId = _typed_id("mnd")
AttemptId = _typed_id("att")
MessageId = _typed_id("msg")
NotificationId = _typed_id("ntf")
DiagnosisId = _typed_id("dx")
PlanId = _typed_id("plan")
ActionId = _typed_id("act")
PromiseId = _typed_id("prm")
IncidentId = _typed_id("inc")
CohortId = _typed_id("coh")
ToolResultId = _typed_id("tr")
EvidenceId = _typed_id("ev")
DocumentId = _typed_id("doc")
LinkId = _typed_id("lnk")
HoldId = _typed_id("hold")
SubscriptionId = _typed_id("sub")

#: Registry of every legal prefix, used by contract tests and by the audit
#: log's reference validator.
ID_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "case", "obl", "payer", "mnd", "att", "msg", "ntf", "dx", "plan", "act",
        "prm", "inc", "coh", "tr", "ev", "doc", "lnk", "hold", "sub",
    }
)

_PREFIX_RE = re.compile(rf"^([a-z]+)_{_BODY}$")


def id_prefix_of(value: str) -> str | None:
    """Return the prefix of an ID, or None if it is not prefix-shaped."""
    match = _PREFIX_RE.match(value)
    return match.group(1) if match else None


def is_valid_id(value: str, prefix: str) -> bool:
    """True when ``value`` is a well-formed ID carrying exactly ``prefix``."""
    return id_prefix_of(value) == prefix
