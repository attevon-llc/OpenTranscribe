"""Snapshot/restore isolation for the `session`/`lockout` cached-state globals.

Extracted from `tests/unit/test_auth_state_degradation.py` (originally landed in commit
`ca7f4c06`, issue #810) so every file that reaches into these module globals can opt in with
one `pytestmark`, not just the one file that happened to define the fixture first.

⚠️ **Honesty note, non-negotiable**: this removes *a* plausible mechanism for #810's flaky
`test_it_re_probes_exactly_at_the_interval_boundary`, and has NOT been shown to remove the
one that actually fired. #810's own CI failure is not locally reproducible (six clean runs
here). Worse: the flaky test cannot be explained by a leaked global at all —
`test_auth_state_degradation.py`'s `TestLockoutStoreDegradation._reset()` runs at the top of
the test body, and `frozen` is derived two lines later from `lockout_module._last_redis_probe`,
a value the test itself just stamped via that same `_reset()`. No neighbour's leftover global
survives that reset to reach the assertion. Any mechanism that actually explains the flake has
to write `_last_redis_probe` *during* the test body from *outside* it — i.e. concurrency — and
none of the sibling files in this cluster spawn a thread. This module is order-dependence
hygiene, not a diagnosis of #810.

Non-autouse on purpose: a file must opt in explicitly via
`pytestmark = pytest.mark.usefixtures("auth_state_globals_restored")` at module scope, so the
isolation applies to module-level tests too, not only ones inside a class whose
`teardown_method` remembered to call it.
"""

from __future__ import annotations

import pytest

from app.auth import lockout as lockout_module
from app.auth import session as session_module

#: Module-level names in `session` and `lockout` that hold per-process CACHED STATE — a
#: Redis handle, the in-memory fallback, whether the store was initialised, when it was
#: last probed. Every one is a value a test can dirty for whichever test runs next in the
#: same worker.
#:
#: Deliberately NOT `_store_lock`: a Lock is machinery, not state, and replacing one
#: mid-suite would be a new bug rather than isolation.
AUTH_STATEFUL_GLOBALS = (
    "_redis_client",
    "_in_memory_store",
    "_store_initialized",
    "_last_redis_probe",
    "_cas_script",
    "_cas_script_client",
)

AUTH_STATE_MODULES = (session_module, lockout_module)


@pytest.fixture
def auth_state_globals_restored():
    """Snapshot the cached-state globals on entry and restore them on exit.

    Restores to the value captured when the test STARTED, never to a hardcoded default —
    that is the actual defect a hand-rolled `teardown_method` has: it clobbers whatever a
    legitimate earlier test (or an outer fixture) had put there, rather than putting it back.
    Opt in with `pytestmark = pytest.mark.usefixtures("auth_state_globals_restored")`.
    """
    saved = [
        (module, name, getattr(module, name))
        for module in AUTH_STATE_MODULES
        for name in AUTH_STATEFUL_GLOBALS
        if hasattr(module, name)
    ]
    yield
    for module, name, value in saved:
        setattr(module, name, value)
