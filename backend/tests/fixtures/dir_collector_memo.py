"""Workaround for the pytest 9.1 conftest-fixture-visibility regression (issue #454).

THE BUG
-------
pytest >= 9.1 defers a conftest's fixtures until that conftest's *Directory
collector* is collected, then matches them to tests by **node object identity**
(``_pytest.fixtures.FixtureManager._matchfactories``: ``if fixturedef.node in
parent_nodes``), popping the conftest off a ``_pending_conftests`` dict so it is
registered exactly once.

Separately, ``Session.collect`` re-collects a directory whenever a command-line
argument *ends* at a file inside it ("for backward compat, files given directly
multiple times on the command line should not be deduplicated" ->
``handle_dupes=False``). Re-collecting a directory builds a **fresh set of child
collectors**. Put the two together::

    pytest tests/unit/test_a.py tests/test_b.py tests/unit/test_c.py
            |                                   |
            |                                   `- hangs off Package(tests/unit) #2
            `- builds Package(tests/unit) #1, which the conftest fixtures bind to

...and every fixture from ``tests/unit/conftest.py`` is invisible to
``test_c.py``. The tests **ERROR at setup** ("fixture 'x' not found"); they do
not fail, so a run can quietly stop exercising whole files' worth of checks
while still reporting a green-looking tally.

Confirmed victims in this repo: ``run_in_clean_process`` and
``revisions_at_or_after`` (``tests/unit/conftest.py``), ``org_context`` and
``organizations_capability_on`` (``tests/api/conftest.py``). Any fixture in any
subdirectory conftest is exposed.

WHAT IT IS NOT
--------------
It is **not** the ``tests/__init__.py`` vs ``tests/unit/__init__.py`` asymmetry.
A minimal reproduction errors identically with both, neither, or either
``__init__.py`` present, and ``tests/api/`` — which has no ``__init__.py`` at
all — is affected too. Bisected to **pytest 9.1.0**: 8.4.2 and 9.0.3 are clean,
9.1.0 and 9.1.1 are not. 9.1.1 is the newest release, so there is no version to
upgrade to.

(``tests/`` must also keep *not* having an ``__init__.py`` for an unrelated
reason: with one, prepend import mode roots ``tests/conftest.py`` at ``backend/``
instead of ``backend/tests/``, ``backend/tests`` never reaches ``sys.path``, and
``pytest_plugins = ["fixtures.mock_llm"]`` dies with ``No module named
'fixtures'`` — the same reason ``--import-mode=importlib`` is unusable here.)

THE FIX
-------
Memoise directory collectors per collection session, so a re-collected directory
yields the **same** child collector objects and the identity the fixture
registration bound to stays valid. Public hooks only, and a no-op on pytest
versions that do not re-create the collectors.

Registered from the root conftest via ``pytest_plugins`` rather than defined
there, so ``unit/test_conftest_fixture_visibility.py`` can load *this exact
module* into a synthetic tree with ``-p`` and pin the real behaviour instead of
a copy that could drift.

Delete this module (and its ``pytest_plugins`` entry) once pytest fixes the
regression upstream — ``test_the_hazard_still_exists_in_this_pytest`` skips with
that instruction the moment it becomes unnecessary.
"""

from __future__ import annotations

from typing import Any

import pytest

#: (parent collector, directory path) -> the collector created for it first.
#: Cleared at the start of every collection, so nothing survives into a second
#: ``perform_collect`` in the same process (``--looponfail``, in-process reruns).
_dir_collectors: dict[tuple[Any, Any], Any] = {}


@pytest.hookimpl(tryfirst=True)
def pytest_collection(session: pytest.Session) -> None:
    """Reset the memo at the start of every collection."""
    del session  # unused; the memo is process-local and collection-scoped
    _dir_collectors.clear()
    return None


@pytest.hookimpl(wrapper=True)
def pytest_collect_directory(path: Any, parent: Any) -> Any:
    """Yield one collector object per (parent, directory) per collection.

    Passing ``None`` straight through matters: ``None`` is how ``norecursedirs``
    (``tests/e2e``) and ``--ignore`` decline a directory, and a memoised ``None``
    would be indistinguishable from "not seen yet".
    """
    collector = yield
    if collector is None:
        return None
    return _dir_collectors.setdefault((parent, path), collector)
