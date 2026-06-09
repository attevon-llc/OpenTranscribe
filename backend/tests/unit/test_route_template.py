"""Unit tests for the bounded route-label helper."""

from app.core.route_template import UNKNOWN_ROUTE
from app.core.route_template import route_label


def test_none_returns_unknown():
    assert route_label(None) == UNKNOWN_ROUTE


def test_empty_string_returns_unknown():
    assert route_label("") == UNKNOWN_ROUTE


def test_template_passthrough():
    assert route_label("/api/files/{file_id}") == "/api/files/{file_id}"


def test_is_pure_same_input_same_output():
    first = route_label("/api/health")
    second = route_label("/api/health")
    assert first == second == "/api/health"


def test_distinct_templates_distinct_labels():
    assert route_label("/api/a") != route_label("/api/b")
