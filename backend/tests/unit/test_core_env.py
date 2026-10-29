"""Tests for the single environment reader (``app.core.env``) and its guard (D4 policy)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from app.core import env as env_mod
from app.core.env import env_bool
from app.core.env import env_float
from app.core.env import env_int
from app.core.env import is_truthy

VAR = "OT_TEST_ENV_VAR"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)


def test_env_int_unset_is_silent_default(caplog):
    with caplog.at_level(logging.WARNING):
        assert env_int(VAR, 7) == 7
    assert caplog.records == []


def test_env_int_parses_with_whitespace(monkeypatch):
    monkeypatch.setenv(VAR, " 12 ")
    assert env_int(VAR, 7) == 12


@pytest.mark.parametrize("raw", ["", "   ", "abc", "1.5", "12x"])
def test_env_int_empty_or_malformed_warns_naming_variable(monkeypatch, caplog, raw):
    monkeypatch.setenv(VAR, raw)
    with caplog.at_level(logging.WARNING):
        assert env_int(VAR, 7) == 7
    assert any(VAR in r.getMessage() for r in caplog.records)


def test_env_int_out_of_range_warns_and_defaults(monkeypatch, caplog):
    monkeypatch.setenv(VAR, "0")
    with caplog.at_level(logging.WARNING):
        assert env_int(VAR, 5, minimum=1) == 5
    assert any(VAR in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setenv(VAR, "99")
    with caplog.at_level(logging.WARNING):
        assert env_int(VAR, 5, maximum=10) == 5
    assert any(VAR in r.getMessage() for r in caplog.records)
    monkeypatch.setenv(VAR, "10")
    assert env_int(VAR, 5, minimum=1, maximum=10) == 10


@pytest.mark.parametrize("raw", ["", "nope", "1,5"])
def test_env_float_empty_or_malformed_warns(monkeypatch, caplog, raw):
    monkeypatch.setenv(VAR, raw)
    with caplog.at_level(logging.WARNING):
        assert env_float(VAR, 0.5) == 0.5
    assert any(VAR in r.getMessage() for r in caplog.records)


def test_env_float_parse_and_range(monkeypatch, caplog):
    monkeypatch.setenv(VAR, "2.5")
    assert env_float(VAR, 0.5) == 2.5
    monkeypatch.setenv(VAR, "-1")
    with caplog.at_level(logging.WARNING):
        assert env_float(VAR, 0.5, minimum=0.0) == 0.5
    assert any(VAR in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setenv(VAR, "nan")
    with caplog.at_level(logging.WARNING):
        assert env_float(VAR, 0.5) == 0.5
    assert any(VAR in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw", ["true", "TRUE", " True ", "1", "yes", "YES", "on", "On"])
def test_env_bool_truthy(monkeypatch, raw):
    monkeypatch.setenv(VAR, raw)
    assert env_bool(VAR, False) is True


@pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "off", " Off "])
def test_env_bool_falsy(monkeypatch, raw):
    monkeypatch.setenv(VAR, raw)
    assert env_bool(VAR, True) is False


def test_env_bool_unset_default_silent(caplog):
    with caplog.at_level(logging.WARNING):
        assert env_bool(VAR, True) is True
    assert caplog.records == []


@pytest.mark.parametrize("raw", ["", "maybe", "2"])
def test_env_bool_empty_or_malformed_warns_and_defaults(monkeypatch, caplog, raw):
    monkeypatch.setenv(VAR, raw)
    with caplog.at_level(logging.WARNING):
        assert env_bool(VAR, True) is True
    assert any(VAR in r.getMessage() for r in caplog.records)


def test_is_truthy():
    assert is_truthy(" Yes ") and is_truthy("1") and is_truthy("on") and is_truthy("TRUE")
    assert not is_truthy("false") and not is_truthy("") and not is_truthy("t")
    assert not is_truthy(None)


_APP = Path(__file__).resolve().parents[2] / "app"
_TRUTHY_ASSIGN = re.compile(r"\b_TRUTHY\s*=")
_TRUTHY_LITERAL = re.compile(r"""\(\s*["']true["']\s*,\s*["']1["']""")


def test_no_truthy_copies_outside_core_env():
    """One truthy set: a re-introduced ``_TRUTHY`` or literal tuple fails here."""
    home = Path(env_mod.__file__).resolve()
    offenders = []
    for path in sorted(_APP.rglob("*.py")):
        if path.resolve() == home:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _TRUTHY_ASSIGN.search(line) or _TRUTHY_LITERAL.search(line):
                offenders.append(f"{path.relative_to(_APP)}:{lineno}")
    assert offenders == [], f"use app.core.env.is_truthy/env_bool instead: {offenders}"
