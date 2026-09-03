"""Phase 1 spine: schema creation and lossless codec round-trips.

These are the foundation the ledger, outbox and audit store are built on: the four
tables exist, and a frozen contract object survives a write/read as its equal.
"""

from __future__ import annotations

import sqlalchemy as sa

from reclaim.contracts.case import RiskCase
from reclaim.contracts.enums import TERMINAL_CASE_STATES
from reclaim.contracts.obligations import Obligation
from reclaim.spine.codec import decode_model, encode_model
from reclaim.spine.db import create_all, get_engine
from reclaim.spine.tables import TERMINAL_STATE_VALUES


def test_create_all_builds_the_four_spine_tables():
    engine = get_engine("sqlite://")
    create_all(engine)
    names = set(sa.inspect(engine).get_table_names())
    assert {"obligations", "risk_cases", "outbox", "audit_log"} <= names


def test_terminal_state_values_match_the_contract_enum():
    """The partial-index predicates are built from TERMINAL_STATE_VALUES; if a new
    terminal state is added to the contract and not reflected here, the ledger would
    keep listing a closed case as at-risk. Walk every row of the enum, not a sample."""
    assert set(TERMINAL_STATE_VALUES) == {state.value for state in TERMINAL_CASE_STATES}


def test_obligation_round_trips_through_the_codec(make_obligation):
    obligation = make_obligation()
    restored = decode_model(Obligation, encode_model(obligation))
    assert restored == obligation


def test_risk_case_round_trips_through_the_codec(make_case):
    case = make_case()
    restored = decode_model(RiskCase, encode_model(case))
    assert restored == case
