"""The capability resolver seam fails CLOSED on an incomplete result (issue #868).

``get_capabilities`` used to merge a resolver's result over
``COMMUNITY_CAPABILITIES``, where almost every key is ``True``. A resolver that
tier-gates ``chat.rag`` but skipped the key on one code path therefore granted
it, with nothing logged. These pin that only an explicit ``True`` from the
resolver grants a capability, and that the community resolver is unchanged.
"""

import logging
from collections.abc import Iterator
from typing import Any
from typing import cast

import pytest
from fastapi import HTTPException

from app.core import capabilities
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import capability_enabled
from app.core.capabilities import get_capabilities
from app.core.capabilities import require_capability
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver

#: Keys the community map turns ON that an external edition would tier-gate —
#: the ones a partial result used to hand out for free.
GATED_ON_BY_DEFAULT = ("chat.rag", "sharing.teams", "collections.shared", "speakers.shared")

ALL_DENIED = dict.fromkeys(COMMUNITY_CAPABILITIES, False)


@pytest.fixture(autouse=True)
def _community_resolver(monkeypatch) -> Iterator[None]:
    # Fresh log-dedupe state per test, so "logged once" is measured per test.
    monkeypatch.setattr(capabilities, "_warned", set(), raising=False)
    reset_capability_resolver()
    yield
    reset_capability_resolver()


def test_community_resolver_result_is_unchanged():
    assert get_capabilities() == COMMUNITY_CAPABILITIES


def test_a_registered_full_map_resolver_is_taken_verbatim():
    full = {**COMMUNITY_CAPABILITIES, "billing": True, "watch_sources": False}
    set_capability_resolver(lambda _req: dict(full))
    assert get_capabilities() == full


def test_partial_result_does_not_inherit_community_grants():
    set_capability_resolver(lambda _req: {"billing": True})

    caps = get_capabilities()

    assert caps["billing"] is True
    for key in GATED_ON_BY_DEFAULT:
        assert caps[key] is False, key
    assert set(caps) == set(COMMUNITY_CAPABILITIES)
    assert sum(caps.values()) == 1


def test_one_missing_key_is_denied_and_the_gate_404s():
    without_rag = {k: v for k, v in COMMUNITY_CAPABILITIES.items() if k != "chat.rag"}
    set_capability_resolver(lambda _req: dict(without_rag))

    caps = get_capabilities()

    assert caps["chat.rag"] is False
    assert {k: v for k, v in caps.items() if k != "chat.rag"} == without_rag
    assert capability_enabled("chat.rag") is False
    dep = require_capability("chat.rag", platform_admin_bypass=False)
    with pytest.raises(HTTPException) as exc:
        dep(cast(Any, None))
    assert exc.value.status_code == 404


def test_missing_keys_are_logged_once_not_per_request(caplog):
    set_capability_resolver(lambda _req: {"billing": True})

    with caplog.at_level(logging.WARNING, logger="app.core.capabilities"):
        get_capabilities()
        get_capabilities()

    omitted = [r for r in caplog.records if "omitted key(s)" in r.getMessage()]
    assert len(omitted) == 1
    assert "chat.rag" in omitted[0].getMessage()


def test_resolver_exception_denies_everything_instead_of_raising(caplog):
    def broken(_req):
        raise RuntimeError("entitlement store unreachable")

    set_capability_resolver(broken)

    with caplog.at_level(logging.ERROR, logger="app.core.capabilities"):
        caps = get_capabilities()

    assert caps == ALL_DENIED
    assert capability_enabled("upload") is False
    assert any("raised" in r.getMessage() for r in caplog.records)


def test_none_result_denies_everything():
    set_capability_resolver(lambda _req: cast(Any, None))
    assert get_capabilities() == ALL_DENIED


def test_non_dict_result_denies_everything():
    set_capability_resolver(lambda _req: cast(Any, [("chat.rag", True)]))
    assert get_capabilities() == ALL_DENIED


@pytest.mark.parametrize("value", ["yes", 1, "false", None])
def test_only_an_explicit_true_grants(value):
    set_capability_resolver(lambda _req: {**COMMUNITY_CAPABILITIES, "chat.rag": value})
    assert get_capabilities()["chat.rag"] is False


def test_unknown_granted_key_passes_through():
    set_capability_resolver(lambda _req: {**COMMUNITY_CAPABILITIES, "future.surface": True})
    caps = get_capabilities()
    assert caps["future.surface"] is True
    assert capability_enabled("future.surface") is True
