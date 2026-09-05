"""Contract tests for the numeric-unit and rail-mechanics contracts."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from reclaim.contracts.canonical import canonical_json
from reclaim.contracts.enums import Rail
from reclaim.contracts.money import Money
from reclaim.contracts.rails import RAIL_SPECS, rail_spec
from reclaim.contracts.units import PValue, Probability, probability, ratio


class _Holder(BaseModel):
    p: Probability


# ---------------------------------------------------------------- units


def test_equal_probabilities_written_differently_hash_identically():
    """0.81 and 0.8100 are the same number; the audit chain must agree."""
    a = canonical_json(_Holder(p=probability(Decimal("0.81"))))
    b = canonical_json(_Holder(p=probability(Decimal("0.8100"))))
    assert a == b


def test_probability_is_canonically_serialisable():
    """A float would raise in canonical_json; a quantised Decimal must not."""
    assert canonical_json(_Holder(p=probability(0.8123456789))) == '{"p":"0.812346"}'


def test_probability_above_one_is_rejected():
    with pytest.raises(ValidationError):
        _Holder(p=Decimal("1.000001"))


def test_an_incidents_fdr_p_value_survives_the_audit_chain():
    """§12.3 records the BH-adjusted p-value so the false-alarm rate is auditable,
    which means it reaches ``canonical_json`` -- and canonical_json rejects floats
    (JC-15). A ``float`` field here is a latent crash at the one moment that
    matters: writing the audit row for a suppressed cohort."""
    from datetime import datetime, timezone

    from reclaim.contracts.obligations import CohortKey, SystemicIncident

    incident = SystemicIncident(
        incident_id="inc_1",
        cohort_key=CohortKey(issuer="HDFC"),
        hypothesis="HDFC auth rate halved",
        opened_at=datetime(2026, 3, 1, 4, 30, tzinfo=timezone.utc),
        attributable_to_us=False,
        fdr_adjusted_p_value=0.0004321987,
    )
    assert canonical_json(incident.model_dump(mode="json")).count('"0.000432199"') == 1


def test_probability_rejects_unquantised_input_at_the_schema_boundary():
    """A raw Decimal with the wrong scale must not slip in unnormalised."""
    assert canonical_json(_Holder(p=Decimal("0.5"))) == '{"p":"0.500000"}'


def test_pvalue_keeps_nine_decimal_places_without_scientific_notation():
    """`str(Decimal("1E-9"))` is "1E-9". The audit chain must not inherit that:
    the serialiser, not CPython's repr threshold, owns the wire form."""

    class _PHolder(BaseModel):
        p: PValue

    assert canonical_json(_PHolder(p=1e-9)) == '{"p":"0.000000001"}'


def test_tiny_probability_also_avoids_scientific_notation():
    assert canonical_json(_Holder(p=Decimal("0.000001"))) == '{"p":"0.000001"}'


def test_ratio_may_exceed_one():
    assert ratio(Decimal("1.5")) == Decimal("1.500000")


# ---------------------------------------------------------------- rails


def test_every_rail_has_a_spec():
    assert set(RAIL_SPECS) == set(Rail)


def test_card_emandate_requires_pre_debit_notification_and_has_a_26h_lead():
    spec = rail_spec(Rail.CARD_EMANDATE)
    assert spec.requires_pre_debit_notification is True
    assert spec.charge_lead_time_hours == 26


def test_upi_autopay_carries_the_rail_level_per_transaction_ceiling():
    spec = rail_spec(Rail.UPI_AUTOPAY)
    assert spec.max_per_transaction == Money.from_rupees(15000)


def test_customer_present_one_time_card_needs_no_pre_debit_notification():
    assert rail_spec(Rail.CARD_ONE_TIME).requires_pre_debit_notification is False


def test_only_mandate_backed_rails_are_recurring():
    for rail, spec in RAIL_SPECS.items():
        if spec.is_recurring:
            assert spec.is_mandate_backed, f"{rail} is recurring but not mandate-backed"
