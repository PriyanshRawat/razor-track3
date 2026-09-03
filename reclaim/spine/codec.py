"""Lossless translation between a frozen contract model and its stored JSON.

The ``data`` column of every spine table holds a contract model serialised with
``model_dump_json`` -- the model's own canonical serialisation, so a decode is its
exact equal, computed fields and all.

The one wrinkle is ``extra="forbid"``. A model with a ``computed_field``
(``RiskCase.is_terminal``, ``RiskCase.at_risk_recognised_at``) serialises that field,
but re-validating a dict that still contains it raises "extra inputs are not
permitted": a computed field is not a settable field. So on the way in, computed
fields are dropped and left to re-derive from the real fields.

``AuditRow`` and ``ActionEnvelope`` are the deliberate exception and do *not* use
this codec to read: their computed ``row_hash`` / ``idempotency_key`` is consumed by
a wrap-validator that *verifies* the stored value equals the re-derived one, so
keeping it turns a read into a tamper check. Those two are decoded by
``model_validate`` directly in ``audit_store``/``outbox``.
"""

from __future__ import annotations

import json
from typing import Type, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


def encode_model(model: BaseModel) -> str:
    """Serialise a frozen contract model to the JSON stored in a ``data`` column."""
    return model.model_dump_json()


def decode_model(cls: Type[M], raw: str | dict) -> M:
    """Rebuild ``cls`` from stored JSON, dropping derived fields before validation."""
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    for computed_name in cls.model_computed_fields:
        data.pop(computed_name, None)
    return cls.model_validate(data)
