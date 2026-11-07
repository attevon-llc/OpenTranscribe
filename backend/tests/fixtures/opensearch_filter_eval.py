"""Evaluate the query-DSL subset the app's filter bodies use against one in-memory document.

For stand-in clusters that must HONOUR the bodies they are sent rather than only record them,
so "the taken-down file is absent from the response" is a statement about what a cluster
obeying the body would return. Covers ``match_all``, ``term`` (plain or ``{"value": ...}``),
``terms``, ``exists`` and ``bool`` (``filter`` / ``must`` / ``must_not`` / ``should`` with
``minimum_should_match``), with OpenSearch's semantics for a multi-valued field (a term matches
when any value equals it) and for ``should`` (required only when there is no ``filter``/``must``,
unless ``minimum_should_match`` says otherwise). Anything else raises, so a body this does not
model fails the test instead of matching by accident.
"""

from __future__ import annotations

from typing import Any


def _values(doc: dict[str, Any], field: str) -> list[Any]:
    value = doc.get(field)
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    """Whether ``doc`` matches ``query``."""
    ((kind, spec),) = query.items()
    if kind == "match_all":
        return True
    if kind == "term":
        ((field, wanted),) = spec.items()
        wanted = wanted["value"] if isinstance(wanted, dict) else wanted
        return wanted in _values(doc, field)
    if kind == "terms":
        ((field, wanted),) = spec.items()
        return any(v in wanted for v in _values(doc, field))
    if kind == "exists":
        return bool(_values(doc, spec["field"]))
    if kind == "bool":
        required = [*_as_list(spec.get("filter", [])), *_as_list(spec.get("must", []))]
        if not all(matches(doc, q) for q in required):
            return False
        if any(matches(doc, q) for q in _as_list(spec.get("must_not", []))):
            return False
        should = _as_list(spec.get("should", []))
        if should:
            minimum = int(spec.get("minimum_should_match", 0 if required else 1))
            return sum(matches(doc, q) for q in should) >= minimum
        return True
    raise AssertionError(f"query kind not modelled by this stand-in: {kind}")


def matches_all(doc: dict[str, Any], clauses: list[dict[str, Any]]) -> bool:
    """Whether ``doc`` matches every clause of a ``bool.filter`` list."""
    return all(matches(doc, clause) for clause in clauses)
