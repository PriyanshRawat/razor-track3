"""Raw PSP decline-code -> canonical ``DeclineClass`` normalizer.

Scope
-----
A **versioned** mapping from the raw decline-code strings a PSP emits to the
canonical ``DeclineClass`` vocabulary in ``reclaim.contracts.decline_taxonomy``.
This is the Phase-0-era slice of §5 / §18.1 item 4: enough rows for a golden test
per code the plan cites, a fail-closed lookup, and a version string. The full
per-PSP table, the ``raw_message_pattern`` disambiguators and the golden-test
*corpus* are still Phase 1.

Why this is not ``decline_taxonomy.lookup_canonical_class``
----------------------------------------------------------
That function is the *contract-layer* lookup: it is PSP-scoped and, by design,
returns the ``UNKNOWN_UNMAPPED`` sentinel for anything it does not know, so that
"fail closed" is a frozen behaviour rather than a Phase-1 choice. This normalizer
is the *caller-facing* one and fails closed the other way: an unmapped code
**raises**. A detector or triage gate calling in here must decide, explicitly,
what to do with a code nobody has classified -- it must not receive a value that
looks like a classification.

Agreement with the freeze
-------------------------
Every canonical target below matches ``SEED_DECLINE_CODE_MAPPINGS`` in
``decline_taxonomy``. ``tests/test_decline_code_normalizer.py`` walks that frozen
table row by row and fails if this map ever diverges from a code it shares. The
map may know *more* codes than the seed table; it may never contradict one.
"""

from __future__ import annotations

from typing import Final, Mapping

from reclaim.contracts.decline_taxonomy import DeclineClass

__all__ = [
    "DECLINE_CODE_MAP",
    "DECLINE_CODE_MAP_VERSION",
    "UnknownDeclineCodeError",
    "is_known_decline_code",
    "normalize_decline_code",
]

#: Semantic version of the mapping table below. Bump MINOR when a row is added,
#: MAJOR when a row's canonical target changes (that re-classifies history).
DECLINE_CODE_MAP_VERSION: Final[str] = "1.0.0"

#: Raw code (lowercase, whitespace-stripped) -> canonical class. ``normalize_
#: decline_code`` canonicalises its input the same way before the lookup, so
#: keys here must already be in that form (a test asserts it).
DECLINE_CODE_MAP: Mapping[str, DeclineClass] = {
    # -- rows that mirror SEED_DECLINE_CODE_MAPPINGS (Stripe docs, plan-cited) --
    "insufficient_funds": DeclineClass.INSUFFICIENT_FUNDS,
    "expired_card": DeclineClass.CARD_EXPIRED,
    "authentication_required": DeclineClass.AUTHENTICATION_REQUIRED,
    "transaction_not_approved": DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS,
    "payment_intent_mandate_invalid": DeclineClass.MANDATE_INVALID,
    "india_recurring_payment_mandate_canceled": DeclineClass.MANDATE_CANCELLED,
    "processing_error": DeclineClass.PROCESSING_ERROR,
    # -- additional spellings the task named; same canonical target ------------
    "card_expired": DeclineClass.CARD_EXPIRED,
}


class UnknownDeclineCodeError(ValueError):
    """A raw decline code has no canonical mapping.

    Fail closed: the caller must handle novelty explicitly (route to the
    diagnostician / a human), not substitute a default class. Subclasses
    ``ValueError`` to match how the contract layer signals bad input.
    """

    def __init__(self, raw_code: str) -> None:
        self.raw_code = raw_code
        super().__init__(
            f"no canonical mapping for decline code {raw_code!r} "
            f"(decline-code map v{DECLINE_CODE_MAP_VERSION}); fail closed -- "
            "do not default, route to diagnosis"
        )


def _canonical_key(raw_code: str) -> str:
    """The form used for both the map keys and the lookup: stripped + casefolded.

    Matches ``decline_taxonomy.lookup_canonical_class``'s ``strip().lower()`` so
    the two lookups agree on what "the same code" means.
    """
    return raw_code.strip().lower()


def normalize_decline_code(raw_code: str) -> DeclineClass:
    """Return the canonical ``DeclineClass`` for ``raw_code``.

    Raises ``UnknownDeclineCodeError`` (a ``ValueError``) if the code is not in
    ``DECLINE_CODE_MAP``. Never returns a sentinel or a guessed default.
    """
    try:
        return DECLINE_CODE_MAP[_canonical_key(raw_code)]
    except KeyError:
        raise UnknownDeclineCodeError(raw_code) from None


def is_known_decline_code(raw_code: str) -> bool:
    """True if ``raw_code`` has a mapping. A non-raising probe for callers that
    want to branch rather than catch."""
    return _canonical_key(raw_code) in DECLINE_CODE_MAP
