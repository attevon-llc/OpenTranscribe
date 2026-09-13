"""The frontend warm-up detector, and the drift guard that would have caught its bug.

Why this file exists, concretely. Two places needed "which module should I fetch to
make Vite compile the SPA?": ``backend/tests/e2e/conftest.py``'s session preflight and
``scripts/run-dev-tests.sh``'s quiesce step. Each carried its own copy. One was
correct. The other matched ``<script src="..." type="module">`` — a shape SvelteKit
never emits, because it references the client entry from an **inline** script.

The consequence was not a loud failure. The broken probe matched nothing on a
perfectly healthy dev server, spent 170 s of a 180 s budget concluding so, printed
``frontend: NOT WARM``, and let the browser suite start against an uncompiled module
graph. Vite's on-demand transform then landed on whichever test ran first. Measured
2026-09-12: **3 failed + 3 errors**, every one a ``wait_for_selector`` timeout on
``.gallery-action-buttons``, every one passing in isolation seconds later — a
signature that reads like flaky tests and is actually a cold server.

The detector now lives in one module. These tests pin the behaviour AND assert the
shell script cannot quietly grow a second copy again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.e2e.frontend_warm import find_entry_modules

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_DEV_TESTS = REPO_ROOT / "scripts" / "run-dev-tests.sh"

#: The real shape, reduced: SvelteKit puts the entry in an INLINE script, so there is
#: no `<script src=... type=module>` anywhere. Captured from the running dev server.
SVELTEKIT_DEV_SHELL = """<!DOCTYPE html>
<html lang="en">
  <head>
    <script src="/theme.js?v=c7b68d42"></script>
    <script>
      {
        __sveltekit_1abcde = { base: "" };
        const element = document.currentScript.parentElement;
        Promise.all([
          import("/node_modules/@sveltejs/kit/src/runtime/client/entry.js"),
          import("/@fs/app/.svelte-kit/generated/client/app.js"),
        ]).then(([kit, app]) => { kit.start(app, element); });
      }
    </script>
  </head>
  <body><div>%sveltekit.body%</div></body>
</html>
"""

#: A served-but-not-a-SPA answer. Must yield nothing, and that is NOT an error —
#: the prod/nginx overlay serves hashed bundles that need no warming at all.
NGINX_ERROR_SHELL = """<html>
<head><title>502 Bad Gateway</title></head>
<body><center><h1>502 Bad Gateway</h1></center><hr><center>nginx</center></body>
</html>
"""


def test_it_finds_the_entry_modules_a_real_sveltekit_dev_shell_references():
    """MUST FIRE. This is the case the broken copy got wrong."""
    mods = find_entry_modules(SVELTEKIT_DEV_SHELL)

    assert mods, (
        "found no modules in a real SvelteKit dev shell — this is exactly the bug that "
        "made run-dev-tests.sh report NOT WARM on every run and hand a cold module "
        "graph to the e2e suite"
    )
    assert "/@fs/app/.svelte-kit/generated/client/app.js" in mods
    assert "/node_modules/@sveltejs/kit/src/runtime/client/entry.js" in mods


def test_the_generated_client_app_is_probed_first_because_it_pulls_the_widest_graph():
    """Order is load-bearing: the caller warms mods[0] and nothing else."""
    mods = find_entry_modules(SVELTEKIT_DEV_SHELL)

    assert mods[0] == "/@fs/app/.svelte-kit/generated/client/app.js", (
        "the generated client app imports the real route modules, so warming it compiles "
        f"the widest graph; probing {mods[0]} first would warm less than intended"
    )


def test_the_old_script_src_type_module_pattern_finds_nothing_here():
    """CONTROL, and the whole point of the file.

    Documents *why* the rejected pattern is rejected, against the same fixture. If
    this ever starts matching, SvelteKit changed how it emits the entry and the
    detector deserves a fresh look rather than a silent pass.
    """
    old_pattern = re.search(
        r'<script[^>]+src="([^"]+)"[^>]*type="module"', SVELTEKIT_DEV_SHELL
    ) or re.search(r'<script[^>]+type="module"[^>]*src="([^"]+)"', SVELTEKIT_DEV_SHELL)

    assert old_pattern is None, (
        "the `<script src type=module>` pattern matched — if SvelteKit now emits that, "
        "re-derive the detector deliberately instead of assuming either shape"
    )


def test_a_non_vite_shell_yields_nothing_rather_than_a_false_entry():
    """MUST STAY CLEAN. Empty means 'nothing to warm', never 'something is broken'."""
    assert find_entry_modules(NGINX_ERROR_SHELL) == []


def test_run_dev_tests_does_not_reimplement_the_detector():
    """DRIFT GUARD — the test that would have caught the original defect.

    The bug was never that one regex was wrong; it was that the fix landed in one of
    two copies. Assert the shell script imports the shared detector and does not
    carry its own `<script ... type="module">` matcher.
    """
    source = RUN_DEV_TESTS.read_text(encoding="utf-8")

    assert "from frontend_warm import find_entry_modules" in source, (
        f"{RUN_DEV_TESTS.name} no longer imports the shared detector — if the quiesce "
        "step grew its own copy again, that is the exact regression this guards"
    )

    reimplemented = re.search(r"<script\[\^>\]\+(src|type)=", source)
    assert reimplemented is None, (
        f"{RUN_DEV_TESTS.name} contains a `<script ... type=module>` matcher again. "
        "SvelteKit references its entry from an INLINE script, so that pattern matches "
        "nothing and silently reports the frontend cold. Use find_entry_modules()."
    )
