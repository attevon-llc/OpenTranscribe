"""escape_like must neutralise every LIKE metacharacter, including the escape char."""

import pytest

from app.utils.sql_like import escape_like


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("50%", "50\\%"),
        ("a_b", "a\\_b"),
        ("trailing\\", "trailing\\\\"),
        ("\\%", "\\\\\\%"),
        ("", ""),
    ],
)
def test_escape_like(raw, expected):
    assert escape_like(raw) == expected


def test_backslash_is_escaped_before_other_metacharacters():
    # Escaping % first would turn "\%" into "\\%", i.e. an escaped backslash then a live wildcard.
    assert escape_like("%") == "\\%"
    assert escape_like("\\") == "\\\\"
    assert escape_like("\\%") == "\\\\" + "\\%"


def test_support_access_org_search_escapes_a_trailing_backslash():
    from unittest.mock import MagicMock

    from app.api.endpoints.support_access import search_target_organizations

    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

    search_target_organizations(q="a\\", limit=5, db=db, _admin=MagicMock())

    pattern = chain.filter.call_args.args[0].right.value
    assert pattern == "%a\\\\%"
