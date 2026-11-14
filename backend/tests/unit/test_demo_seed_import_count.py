"""``demo_seed import`` fails unless the demo account owns exactly the manifest's items (#628).

The design's "at least one file" check has no setting of its own: the seed bundle's manifest is
the count, and ``load_bundle`` already refuses an empty one. These tests drive the real
``_import`` with the database and storage steps stubbed, so they need no stack.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.scripts.demo_seed import __main__ as seed

ITEMS = [SimpleNamespace(id="a", license="CC0-1.0"), SimpleNamespace(id="b", license="CC0-1.0")]


@pytest.fixture
def stubbed(monkeypatch):
    from app.core.config import settings
    from app.db import base

    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "DEMO_USER_EMAIL", "demo@example.com")
    monkeypatch.setattr(settings, "DEMO_USER_PASSWORD", "test-only-value")

    @contextmanager
    def session():
        yield SimpleNamespace(commit=lambda: None)

    monkeypatch.setattr(base, "SessionLocal", session)
    monkeypatch.setattr(seed, "load_bundle", lambda _bundle: ("2026.11.1", ITEMS))
    monkeypatch.setattr(seed, "load_conversations", lambda _bundle, _ids: [])
    monkeypatch.setattr(seed, "_ensure_demo_user", lambda _db, _e, _p: 7)
    monkeypatch.setattr(seed, "_seed_item", lambda item, _v, _u: f"uuid-{item.id}")
    monkeypatch.setattr(seed, "_file_into_collections", lambda *_a: None)
    monkeypatch.setattr(seed, "_seed_conversations", lambda *_a: None)
    monkeypatch.setattr(seed, "_pending_redactions", lambda _db: 0)
    return monkeypatch


def test_a_seed_that_left_the_account_short_fails(stubbed, tmp_path, capsys):
    """Must fire: a reset must not open the demo with fewer files than the bundle lists."""
    stubbed.setattr(seed, "_seeded_file_count", lambda _db, _user: 1, raising=False)

    assert seed.main(["import", "--bundle", str(tmp_path)]) == seed.EXIT_FAILED
    assert "owns 1 file(s), the bundle lists 2" in capsys.readouterr().err


def test_an_empty_account_fails(stubbed, tmp_path):
    stubbed.setattr(seed, "_seeded_file_count", lambda _db, _user: 0, raising=False)

    assert seed.main(["import", "--bundle", str(tmp_path)]) == seed.EXIT_FAILED


def test_the_account_owning_every_item_passes(stubbed, tmp_path, capsys):
    """Must stay clean, and the count is asked for the demo account, not every user."""
    asked: list[int] = []

    def count(_db, user: int) -> int:
        asked.append(user)
        return 2

    stubbed.setattr(seed, "_seeded_file_count", count, raising=False)

    assert seed.main(["import", "--bundle", str(tmp_path)]) == seed.EXIT_OK
    assert asked == [7]
    assert "2 item(s)" in capsys.readouterr().out
