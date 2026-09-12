"""Find the entry modules a Vite/SvelteKit dev shell references.

THE SINGLE SOURCE OF TRUTH for that detection. It lives here because two callers
need it and, for a while, each carried its own copy — one correct, one not:

  * ``backend/tests/e2e/conftest.py``'s ``_warm_frontend_module_graph`` (session
    preflight), and
  * ``scripts/run-dev-tests.sh``'s quiesce step (before the browser suite).

⚠️ **Why the obvious pattern is wrong.** SvelteKit references its client entry from
an **inline** script, not from ``<script src="..." type="module">``. A probe matching
the latter finds nothing on a perfectly healthy dev server and reports NOT WARM on
*every single run*. That draft was written, rejected and documented in
``conftest.py`` — and then shipped anyway in ``run-dev-tests.sh``, where it burned
170 s of a 180 s quiesce budget, declared the frontend cold, and let the e2e suite
start against an uncompiled module graph. Vite's on-demand transform then landed on
whichever test ran first, surfacing as ``gallery_page`` timing out on
``.gallery-action-buttons`` — a fixture timeout that reads like a broken test rather
than a cold server. Measured 2026-09-12: 3 failed + 3 errors, all
``wait_for_selector`` timeouts, all passing in isolation seconds later.

So: match the module URLs the shell actually contains, wherever they appear.

An empty result is **not** an error — the prod/nginx overlay serves hashed bundles
that need no warming at all. Callers must treat "no modules" as "nothing to do".
"""

from __future__ import annotations

import re

#: Module URLs a Vite dev shell references, inline or otherwise. Deliberately not
#: anchored to a ``<script>`` tag: see this module's docstring for what that cost.
_ENTRY_MODULE_RE = re.compile(r'["\'](/(?:@fs|@vite|src|node_modules)/[^"\']+\.js)["\']')

#: The generated client app imports the real route modules, so warming it pulls the
#: widest graph of anything the shell references. Prefer it, then shortest URL.
_GENERATED_CLIENT_APP = "generated/client/app.js"


def find_entry_modules(shell_html: str) -> list[str]:
    """Return referenced module paths, widest-graph-first.

    Args:
        shell_html: The HTML body served at the SPA root.

    Returns:
        Absolute module paths (leading ``/``), best warm-up candidate first. Empty
        when the shell is not a Vite dev shell — a served-but-not-a-SPA answer such
        as an nginx error page, or the prod overlay's hashed bundles. Empty means
        "nothing to warm", never "something is broken".
    """
    mods = _ENTRY_MODULE_RE.findall(shell_html)
    # dict.fromkeys: de-duplicate while keeping first-seen order stable, so the sort
    # below is deterministic for shells that reference a module more than once.
    mods = list(dict.fromkeys(mods))
    mods.sort(key=lambda u: (0 if _GENERATED_CLIENT_APP in u else 1, len(u)))
    return mods
