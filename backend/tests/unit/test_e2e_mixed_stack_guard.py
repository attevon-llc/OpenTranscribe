"""``stack_urls.mixed_stack_problem`` — the "impossible to enter silently" guard (#965).

A browser driven against one E2E stack while the API helpers talk to another is a
silent, confusing failure mode (issue #965: a bulk-delete test uploaded its files to
one backend and looked for them in another stack's UI). ``tests/e2e/conftest.py``'s
autouse session preflight calls ``mixed_stack_problem`` and ``pytest.exit``s loudly the
moment it disagrees, rather than letting the mismatch surface as an arbitrary later
test failure.

``stack_urls.py`` is dependency-free by design (no playwright, no requests, no
``tests.conftest`` import side effects), so these tests run in milliseconds against
pure string input, the same reason ``test_frontend_warm_probe.py`` and
``test_visual_diff_fraction.py`` import their conftest-adjacent helpers the same way.
"""

from __future__ import annotations

import pytest

from tests.e2e.stack_urls import mixed_stack_problem

pytestmark = pytest.mark.unit


class TestMustStayClean:
    """Pairs that DO belong to one stack — the guard must never fire on these."""

    def test_live_stack_default_pair_is_clean(self) -> None:
        assert mixed_stack_problem("http://localhost:5173", "http://localhost:5174") is None

    def test_matching_port_offset_pair_is_clean(self) -> None:
        """A frontend/backend pair from the SAME --fresh --port-offset 200 deployment."""
        assert mixed_stack_problem("http://localhost:5373", "http://localhost:5374") is None

    def test_large_offset_pair_is_clean(self) -> None:
        assert mixed_stack_problem("http://localhost:6173", "http://localhost:6174") is None


class TestMustFire:
    """Pairs that CANNOT belong to one stack — the guard must always catch these."""

    def test_browser_on_fresh_stack_api_on_live_stack_is_caught(self) -> None:
        """The exact incident issue #965 describes: browser on 5373, API on 5174."""
        problem = mixed_stack_problem("http://localhost:5373", "http://localhost:5174")

        assert problem is not None
        assert "5373" in problem
        assert "5174" in problem

    def test_browser_on_live_stack_api_on_fresh_stack_is_caught(self) -> None:
        """The reverse direction: browser on the live stack, API helpers on --fresh."""
        problem = mixed_stack_problem("http://localhost:5173", "http://localhost:5374")

        assert problem is not None
        assert "5173" in problem
        assert "5374" in problem

    def test_different_hosts_are_caught(self) -> None:
        problem = mixed_stack_problem("http://localhost:5173", "http://otherhost:5174")

        assert problem is not None
        assert "host" in problem.lower()

    def test_equal_ports_are_caught(self) -> None:
        """Frontend and backend can never share a port on any stack this tooling makes."""
        problem = mixed_stack_problem("http://localhost:5173", "http://localhost:5173")

        assert problem is not None
        assert "5173" in problem
