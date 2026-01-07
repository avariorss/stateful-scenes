"""Matching logic: is a scene currently active?

A scene is considered active when *every* entity listed in the scene definition
matches the desired `state` and any desired attributes specified for that
entity.

We only compare the keys present in the scene YAML; extra attributes on the
entity are ignored.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from homeassistant.core import State

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MatchOptions:
    number_tolerance: int = 1
    ignore_unavailable: bool = False
    ignore_attributes: bool = False


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _values_match(expected: Any, actual: Any, *, tol: int) -> bool:
    """Compare expected vs actual with numeric tolerance support."""
    # Fast path: exact match
    if expected == actual:
        return True

    # Numeric tolerance (check FIRST to handle int vs float comparisons)
    if _is_number(expected) and _is_number(actual):
        exp_f, act_f = float(expected), float(actual)
        
        # Handle special float values (NaN, Infinity)
        if not (math.isfinite(exp_f) and math.isfinite(act_f)):
            return exp_f == act_f  # NaN != NaN, inf == inf
        
        return abs(exp_f - act_f) <= float(tol)

    # String comparison (case-insensitive)
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.casefold() == actual.casefold()

    # Lists / tuples: element-wise (same type required)
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return False
        return all(_values_match(e, a, tol=tol) for e, a in zip(expected, actual, strict=True))

    # Dicts: compare keys present in expected (same type required)
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k, v in expected.items():
            if k not in actual:
                return False
            if not _values_match(v, actual[k], tol=tol):
                return False
        return True

    # Everything else: no match
    return False


def entity_matches(
    state: State | None,
    expected: dict[str, Any],
    *,
    opts: MatchOptions,
) -> bool | None:
    """Return True if entity matches, False if not, None if ignored.

    If ignore_unavailable=True and the entity is unavailable/unknown, we return
    None (ignored) instead of False.
    """
    if state is None:
        return None if opts.ignore_unavailable else False

    if opts.ignore_unavailable and state.state in {"unavailable", "unknown"}:
        return None

    # State match (string comparisons are case-insensitive for robustness)
    expected_state = expected.get("state")
    if expected_state is not None:
        actual_state = state.state

        # Normalize to string for YAML values like 0/1/True/False
        exp_s = str(expected_state)
        act_s = str(actual_state)
        if exp_s.casefold() != act_s.casefold():
            return False

        # If both are "off", we treat attributes as don't-care.
        # This mirrors the original integration behavior and avoids false
        # negatives when lights include attributes while off.
        if act_s.casefold() == "off":
            return True

    if opts.ignore_attributes:
        return True

    # Attribute match: compare only keys provided by the scene definition
    for attr_key, expected_val in expected.items():
        if attr_key == "state":
            continue
        if attr_key not in state.attributes:
            _LOGGER.debug(
                "Entity %s missing expected attribute '%s'",
                state.entity_id,
                attr_key,
            )
            return False
        actual_val = state.attributes.get(attr_key)
        matches = _values_match(expected_val, actual_val, tol=opts.number_tolerance)
        if not matches:
            _LOGGER.debug(
                "Entity %s attribute '%s' mismatch: expected=%r (type=%s), actual=%r (type=%s), tolerance=%d",
                state.entity_id,
                attr_key,
                expected_val,
                type(expected_val).__name__,
                actual_val,
                type(actual_val).__name__,
                opts.number_tolerance,
            )
            return False

    return True