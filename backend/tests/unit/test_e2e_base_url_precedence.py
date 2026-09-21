"""``tests/e2e/pytest.ini`` must not pre-empt ``--base-url``/``E2E_FRONTEND_URL`` (#965).

``backend/tests/e2e/pytest.ini`` used to set ``base_url = http://localhost:5173``.
pytest-base-url's own ``pytest_configure`` does
``config.option.base_url = config.getoption("base_url") or config.getini("base_url")``
— so an ini default is written into the OPTION exactly as if ``--base-url`` had been
passed on the command line. ``conftest.py``'s ``base_url`` fixture checks
``request.config.getoption("base_url")`` before falling back to ``E2E_FRONTEND_URL``, so
with the ini default in place that check is never ``None`` and the env var branch is
unreachable dead code — the exact bug ``TestBaseUrlPrecedence`` below must catch RED.

Follows the config-consistency idiom of ``test_pytest_config_consistency.py`` for the
static half, and uses pytest's own bundled ``pytester`` fixture (a real, isolated pytest
subprocess) to prove this is a genuine pytest-base-url/pytest.ini interaction, not merely
a claim about ``conftest.py``'s own code — that fixture's logic was already "correct" in
isolation; the regression came entirely from something external overriding what it saw.

The mixed-stack guard this issue also adds (``tests/e2e/stack_urls.py``) is covered
separately in ``test_e2e_mixed_stack_guard.py`` — that module is new code with no
"before" state to watch fail, so it does not belong in this red-first file.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2]
_E2E_PYTEST_INI = _BACKEND / "tests" / "e2e" / "pytest.ini"

#: The exact fixture logic `tests/e2e/conftest.py`'s `base_url` fixture implements —
#: reproduced here (not imported) so the pytester subprocess below never has to load
#: conftest.py's heavy playwright/backend-import chain just to prove the ini/plugin
#: interaction. Keep in sync with conftest.py's `base_url` fixture if that ever changes.
_BASE_URL_FIXTURE_CONFTEST = """
import os
import pytest

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")


@pytest.fixture(scope="session")
def base_url(request):
    from_flag = request.config.getoption("base_url", default=None)
    if from_flag:
        return str(from_flag)
    return FRONTEND_URL
"""

_PROBE_TEST = """
def test_probe(base_url):
    print(f"RESOLVED_BASE_URL={base_url}")
"""


def _make_probe_pytester(pytester: pytest.Pytester) -> None:
    """Wire up a pytester sandbox with the REAL pytest.ini plus the fixture stand-in."""
    pytester.makefile(".ini", pytest=_E2E_PYTEST_INI.read_text())
    pytester.makeconftest(_BASE_URL_FIXTURE_CONFTEST)
    pytester.makepyfile(test_probe=_PROBE_TEST)


class TestPytestIniDoesNotShadowBaseUrl:
    """The static claim: the real ini file must not pre-populate `base_url`."""

    def test_pytest_ini_sets_no_base_url_default(self) -> None:
        """This is the literal root cause of issue #965.

        pytest-base-url treats an ini ``base_url`` exactly like the ``--base-url``
        flag (see this module's docstring), so ANY value here permanently defeats
        ``E2E_FRONTEND_URL``. There is no correct non-empty value for this key.
        """
        parser = configparser.ConfigParser()
        parser.read(_E2E_PYTEST_INI)

        assert "base_url" not in parser["pytest"], (
            "tests/e2e/pytest.ini sets a [pytest] base_url default, which pytest-base-url's "
            "pytest_configure writes into config.option.base_url unconditionally — this makes "
            "E2E_FRONTEND_URL dead code every time --base-url is not passed explicitly "
            "(issue #965). The sane default for a bare invocation belongs in conftest.py's "
            "FRONTEND_URL constant instead."
        )


class TestBaseUrlPrecedence:
    """The real pytest-base-url + pytest.ini interaction, run as an isolated subprocess."""

    def test_e2e_frontend_url_env_var_is_honoured_without_flag(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE bug: with no --base-url flag, E2E_FRONTEND_URL must win over the ini.

        Watched RED against the pre-fix ``pytest.ini`` (via a ``git archive HEAD``
        checkout per the repo's red-first convention): pytest-base-url's
        ``pytest_configure`` read the ini's ``base_url = http://localhost:5173`` and
        wrote it into ``config.option.base_url``, so the probe fixture's
        ``getoption("base_url")`` check returned that value instead of ``None`` and
        the env var below was never consulted — resolving to
        ``http://localhost:5173`` instead of the env value asserted here.
        """
        monkeypatch.setenv("E2E_FRONTEND_URL", "http://localhost:9999")
        _make_probe_pytester(pytester)

        result = pytester.runpytest_subprocess("-s")

        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*RESOLVED_BASE_URL=http://localhost:9999*"])

    def test_explicit_base_url_flag_still_wins_over_env_var(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented precedence's OTHER half: --base-url must still beat the env var.

        Fixing issue #965 must not regress the #431 fix this module's docstring
        describes — a caller explicitly aiming at a specific stack always wins.
        """
        monkeypatch.setenv("E2E_FRONTEND_URL", "http://localhost:9999")
        _make_probe_pytester(pytester)

        result = pytester.runpytest_subprocess("-s", "--base-url", "http://localhost:8888")

        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*RESOLVED_BASE_URL=http://localhost:8888*"])
