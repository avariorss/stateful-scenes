"""Matching logic: is a scene currently active?

A scene is considered active when *every* entity listed in the scene definition
matches the desired `state` and any desired attributes specified for that
entity.

We only compare the keys present in the scene YAML; extra attributes on the
entity are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import State


@dataclass(frozen=True, slots=True)
class MatchOptions:
    number_tolerance: int = 1
    ignore_unavailable: bool = False
    ignore_attributes: bool = False


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _values_match(expected: Any, actual: Any, *, tol: int) -> bool:
    """Compare expected vs actual with numeric tolerance support."""

    # Strings: case-insensitive (matches original integration behavior)
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.casefold() == actual.casefold()

    # Numeric tolerance
    if _is_number(expected) and _is_number(actual):
        return abs(float(expected) - float(actual)) <= float(tol)

    # Lists / tuples: element-wise
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return False
        return all(_values_match(e, a, tol=tol) for e, a in zip(expected, actual))

    # Dicts: compare keys present in expected
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k, v in expected.items():
            if k not in actual:
                return False
            if not _values_match(v, actual[k], tol=tol):
                return False
        return True

    return expected == actual


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
            return False
        if not _values_match(expected_val, state.attributes.get(attr_key), tol=opts.number_tolerance):
            return False

    return True
