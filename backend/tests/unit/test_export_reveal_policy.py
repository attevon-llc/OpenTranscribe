"""One decision for what an export may reveal (issue #1278, building on #673).

``export_locked`` was a copy-pasted branch in two endpoints; every export surface now calls
``resolve_export_reveal``. Each case below pairs a locked policy with an unlocked control, so
"always return the empty set" cannot pass.
"""

from __future__ import annotations

import pytest

from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.export_policy import resolve_export_reveal

pytestmark = pytest.mark.unit


def _cfg(*, locked: bool, forced: frozenset[str] = frozenset()) -> EffectiveRedactionConfig:
    return EffectiveRedactionConfig(
        enabled=True,
        enabled_categories={"profanity", "pii"},
        locked_categories=set(forced),
        export_locked=locked,
    )


def _reveal(cfg: EffectiveRedactionConfig, **kw: bool) -> set[str]:
    args = {"reveal_requested": True, "is_owner": True, "admin_reveal_allowed": False}
    args.update(kw)
    return resolve_export_reveal(cfg, **args)


def test_export_locked_wins_over_a_reveal_request_by_the_owner() -> None:
    assert _reveal(_cfg(locked=True)) == set()


def test_export_locked_wins_over_an_admin_reveal() -> None:
    assert _reveal(_cfg(locked=True), is_owner=False, admin_reveal_allowed=True) == set()


def test_unlocked_owner_reveal_returns_the_enabled_categories() -> None:
    assert _reveal(_cfg(locked=False)) == {"profanity", "pii"}


def test_unlocked_reveal_still_leaves_admin_forced_categories_masked() -> None:
    assert _reveal(_cfg(locked=False, forced=frozenset({"pii"}))) == {"profanity"}


def test_a_non_owner_without_an_admin_bypass_reveals_nothing() -> None:
    assert _reveal(_cfg(locked=False), is_owner=False) == set()


def test_an_admin_bypass_lets_a_non_owner_reveal_when_unlocked() -> None:
    assert _reveal(_cfg(locked=False), is_owner=False, admin_reveal_allowed=True) == {
        "profanity",
        "pii",
    }


def test_no_request_means_no_reveal() -> None:
    assert _reveal(_cfg(locked=False), reveal_requested=False) == set()
