"""Golden + fail-closed tests for the raw-decline-code normalizer.

The normalizer is a versioned lookup table that lives outside
``reclaim.contracts`` (it is data, not a domain model). Its canonical targets are
required to agree with the frozen ``SEED_DECLINE_CODE_MAPPINGS`` in
``reclaim.contracts.decline_taxonomy`` -- ``test_normalizer_reproduces_every_frozen_seed_row``
is the guard that they never drift apart.
"""

from __future__ import annotations

import pytest

from reclaim.contracts.decline_taxonomy import (
    SEED_DECLINE_CODE_MAPPINGS,
    DeclineClass,
)
from reclaim.normalize.decline_codes import (
    DECLINE_CODE_MAP,
    DECLINE_CODE_MAP_VERSION,
    UnknownDeclineCodeError,
    is_known_decline_code,
    normalize_decline_code,
)

# --- one golden assertion per known raw code -------------------------------

_GOLDEN: tuple[tuple[str, DeclineClass], ...] = (
    ("insufficient_funds", DeclineClass.INSUFFICIENT_FUNDS),
    ("expired_card", DeclineClass.CARD_EXPIRED),
    ("card_expired", DeclineClass.CARD_EXPIRED),
    ("authentication_required", DeclineClass.AUTHENTICATION_REQUIRED),
    ("transaction_not_approved", DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS),
    ("payment_intent_mandate_invalid", DeclineClass.MANDATE_INVALID),
    ("india_recurring_payment_mandate_canceled", DeclineClass.MANDATE_CANCELLED),
    ("processing_error", DeclineClass.PROCESSING_ERROR),
)


@pytest.mark.parametrize("raw_code, expected", _GOLDEN, ids=[c for c, _ in _GOLDEN])
def test_golden_code_normalizes_to_its_canonical_class(raw_code, expected):
    assert normalize_decline_code(raw_code) is expected


# --- fail closed: an unmapped code raises, never defaults ------------------

def test_unknown_code_raises_rather_than_defaulting():
    with pytest.raises(UnknownDeclineCodeError):
        normalize_decline_code("some_psp_specific_code_we_have_never_seen")


def test_unknown_code_error_is_a_value_error_and_names_the_code():
    with pytest.raises(ValueError) as exc:
        normalize_decline_code("mystery_code")
    assert "mystery_code" in str(exc.value)
    assert getattr(exc.value, "raw_code", None) == "mystery_code"


def test_blank_code_raises():
    with pytest.raises(UnknownDeclineCodeError):
        normalize_decline_code("   ")


# --- input canonicalisation ---------------------------------------------------

@pytest.mark.parametrize("variant", ["INSUFFICIENT_FUNDS", "Insufficient_Funds", "  insufficient_funds\n"])
def test_lookup_is_case_and_surrounding_whitespace_insensitive(variant):
    assert normalize_decline_code(variant) is DeclineClass.INSUFFICIENT_FUNDS


# --- structural guards ------------------------------------------------------

def test_map_version_is_a_nonempty_string():
    assert isinstance(DECLINE_CODE_MAP_VERSION, str) and DECLINE_CODE_MAP_VERSION.strip()


def test_every_map_entry_is_a_canonical_declineclass_under_a_normalised_key():
    assert DECLINE_CODE_MAP, "the map must not be empty"
    for raw_code, canonical in DECLINE_CODE_MAP.items():
        assert raw_code == raw_code.strip().lower(), f"key {raw_code!r} is not pre-normalised"
        assert isinstance(canonical, DeclineClass), f"{raw_code!r} maps to a non-DeclineClass"


def test_normalizer_reproduces_every_frozen_seed_row():
    """Every ``SEED_DECLINE_CODE_MAPPINGS`` row must round-trip through the
    normalizer to the same canonical class. This is the anti-drift guard: the
    normalizer is allowed to know *more* codes than the seed table, never to
    disagree with one it shares."""
    for row in SEED_DECLINE_CODE_MAPPINGS:
        assert normalize_decline_code(row.raw_code) is row.canonical_class, (
            f"normalizer disagrees with frozen seed row {row.raw_code!r}"
        )


def test_is_known_decline_code_probe():
    assert is_known_decline_code("insufficient_funds") is True
    assert is_known_decline_code("  AUTHENTICATION_REQUIRED ") is True
    assert is_known_decline_code("not_a_real_code") is False
