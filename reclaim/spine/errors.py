"""The spine's own exception taxonomy.

These name the *domain* failures a caller is expected to catch and respond to --
a double-booked obligation, an illegal state jump, a blocked second debit -- as
opposed to a programming mistake (opening a case in the wrong state), which stays a
plain ``ValueError``. Keeping them in one leaf module means every spine module can
raise them without importing each other.
"""

from __future__ import annotations

__all__ = [
    "SpineError",
    "CaseNotFound",
    "DuplicateActiveCase",
    "DoubleDebitBlocked",
    "IllegalTransition",
]


class SpineError(Exception):
    """Base class for every domain error the spine raises."""


class CaseNotFound(SpineError):
    """A case id was referenced that the ledger has never seen."""


class DuplicateActiveCase(SpineError):
    """A second live case was opened on an obligation that already has one.

    The ledger holds one row per obligation (§4/§13). This is the friendly form of
    the ``uq_risk_case_active_obligation`` partial UNIQUE index.
    """


class DoubleDebitBlocked(SpineError):
    """A debit was enqueued for an ``(obligation, attempt_sequence)`` that is already
    scheduled -- the CONTRACTS.md Q1 cross-case hole, caught at the outbox."""


class IllegalTransition(SpineError):
    """A case state change absent from ``ALLOWED_CASE_TRANSITIONS`` (§9.1)."""
