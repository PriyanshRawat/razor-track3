"""Time handling contract.

Two rules, frozen:

1. **Every timestamp crossing a module boundary is timezone-aware UTC.** A naive
   datetime is a validation error, not a coercion. Quiet-hours enforcement
   (invariant #3: "no contact outside quiet hours, **in any timezone**") is the
   reason: a naive datetime silently assumes the server's zone, and the server's
   zone is not the payer's.
2. **Local time is derived, never stored.** The payer's IANA zone lives on the
   payer record; the policy engine converts UTC -> payer-local at evaluation time.

``IST`` is provided because RBI windows and the default quiet hours (09:00-19:00
IST, §14.1) are expressed in it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, PlainSerializer

__all__ = [
    "IST",
    "RFC3339_FORMAT",
    "UtcDatetime",
    "to_rfc3339",
    "to_utc",
    "utcnow",
]

IST = timezone(timedelta(hours=5, minutes=30))
RFC3339_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def to_utc(value: datetime) -> datetime:
    """Normalise an aware datetime to UTC. Rejects naive input."""
    if value.tzinfo is None:
        raise ValueError(
            "Naive datetime rejected. All RECLAIM timestamps must be "
            "timezone-aware; quiet-hours correctness depends on it."
        )
    return value.astimezone(timezone.utc)


def to_rfc3339(value: datetime) -> str:
    """Canonical wire format: UTC, microsecond precision, trailing Z.

    Used by the audit chain's canonical serialiser, so the format is a contract:
    changing it changes every row hash.
    """
    return to_utc(value).strftime(RFC3339_FORMAT)


#: An aware datetime, normalised to UTC on validation and serialised as RFC3339 Z.
UtcDatetime = Annotated[
    AwareDatetime,
    AfterValidator(to_utc),
    PlainSerializer(to_rfc3339, return_type=str, when_used="always"),
]


def utcnow() -> datetime:
    """Current time, aware UTC. Injected in Phase 1 so tests can freeze it."""
    return datetime.now(timezone.utc)
