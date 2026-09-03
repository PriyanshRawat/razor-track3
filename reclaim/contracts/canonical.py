"""Canonical serialisation.

One function, and it is load-bearing twice over:

* the audit chain's row hashes (§15) are computed over its output, so any change to
  it invalidates every previously written chain;
* idempotency keys (invariant #1) are derived from it, so two logically identical
  action parameter sets must produce byte-identical bytes.

Rules
-----
* keys sorted;
* no insignificant whitespace;
* ``ensure_ascii=False`` with UTF-8 encoding, so Devanagari template variables hash
  as themselves rather than as escape sequences;
* floats are **rejected**. A float has no canonical decimal form, so a chain built
  over floats is not reproducibly verifiable. Money is integer paise
  (``reclaim.contracts.money``); statistics are rounded to a fixed number of
  decimals and serialised as strings before they reach here.

This module deliberately imports nothing from the rest of RECLAIM so that both
``actions`` and ``audit`` can depend on it without a cycle.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

__all__ = [
    "CANONICAL_JSON_VERSION",
    "canonical_bytes",
    "canonical_json",
    "digest",
]

#: Bumping this is a MAJOR contract change: it re-hashes the world.
CANONICAL_JSON_VERSION = "1.0.0"


def _default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "model_dump"):  # pydantic BaseModel
        return obj.model_dump(mode="json")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=repr)
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"{type(obj).__name__} is not canonically serialisable")


def _reject_floats(value: Any, path: str = "$") -> None:
    """Depth-first float check with a path in the error, so the offending field is
    named rather than merely reported."""
    if isinstance(value, float):
        raise TypeError(
            f"float at {path} is not canonically serialisable; use integer paise "
            "for money or a fixed-precision string for statistics"
        )
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_floats(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _reject_floats(v, f"{path}[{i}]")


def canonical_json(payload: Any) -> str:
    """Deterministic JSON text for ``payload``."""
    normalised = json.loads(json.dumps(payload, default=_default))
    _reject_floats(normalised)
    return json.dumps(
        normalised,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(payload: Any) -> bytes:
    return canonical_json(payload).encode("utf-8")


def digest(payload: Any) -> str:
    """SHA-256 hex digest of the canonical form. Used for ``inputs_digest``,
    ``tool_result_digest`` and idempotency keys."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
