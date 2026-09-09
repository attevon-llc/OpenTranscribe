"""Shared Playwright wait budgets for the E2E suite.

Not a test module (no ``test_`` prefix), so pytest imports it and collects nothing from it.

**Why these are constants and not literals.** Before this file, ``wait_for_selector("#email",
timeout=...)`` appeared **65 times across 15 files** at two different values (10000 and 15000)
with no stated reason for either. A budget copied 65 times cannot be revised — it can only
drift, and when it is wrong it is wrong in 65 places at once. The repo's rule (see
``backend/tests/CLAUDE.md``) is: name the constant, put the measurement beside it.
"""

from __future__ import annotations

#: How long to wait for the login form to exist after navigating to /login.
#:
#: 30 s, raised from a copy-pasted 10000/15000, and it is NOT a flake workaround — the
#: contract genuinely changed. ``login/+page.svelte`` used to paint the credential form
#: immediately against local-only placeholder defaults, then insert the SSO/PKI/register
#: elements once ``getAuthMethods()`` resolved. That shifted the layout under the user (and
#: under Playwright's click), so the card is now gated on ``authMethodsLoaded`` and renders
#: once, in its final shape. ``#email`` therefore no longer appears on the first paint: it
#: appears after the auth-methods round trip.
#:
#: So the honest budget is "app shell + one API call", and the app shell is itself gated on
#: ``{#if $authReady}`` behind ``initAuth()``'s ``GET /auth/session`` — which carries a **60 s**
#: axios timeout. 10 s was already under-budget for that even before the change; it simply
#: happened to pass while the form rendered ahead of the fetch. Under the full gate (3
#: Playwright workers, 48 pytest workers, a backend under load) it timed out and produced 3
#: failures in ``test_auth_buttons.py`` on 2026-09-06 that had nothing to do with auth.
#:
#: 30 s matches the sibling app-shell waits already in ``conftest.py``
#: (``.gallery-action-buttons``, ``.gallery-header-right``) and ``test_search.py``'s
#: ``.search-page``. Reaching it still means a real failure, and one worth 30 s of evidence.
LOGIN_FORM_READY_MS = 30_000

#: How long to wait for the authenticated app shell to paint — ``.gallery-action-buttons``,
#: ``.gallery-header-right``, ``.search-page`` and the other post-login landmarks.
#:
#: **The drift this file was created to stop had already recurred by the time it landed.**
#: ``#email`` was centralised above, but the two other app-shell selectors stayed raw literals
#: at *three different values across 23 call sites*: 10000 in ``conftest.py``'s shared fixture,
#: 15000 in ``test_gallery_actions.py`` / ``test_responsive.py`` / ``test_search.py``, and 30000
#: everywhere else. ``test_search.py:42`` was raised to 30000 at some point and ``:361`` was
#: not — which is the drift, visible in one file.
#:
#: The budget is the same shape as ``LOGIN_FORM_READY_MS`` and for the same reason: these
#: selectors sit behind ``+layout.svelte``'s ``{#if $authReady}``, so reaching them costs the
#: app shell plus ``initAuth()``'s ``GET /auth/session``. Anything under that is not measuring
#: the page, it is measuring how loaded the machine is.
APP_SHELL_READY_MS = 30_000


#: How long to wait for a value written SERVER-SIDE to be visible after a fresh page load.
#:
#: Distinct from ``APP_SHELL_READY_MS`` on purpose: by the time this budget starts, the page
#: has already been gated on its own content selector (``.transcript-segment``, 25 s), so the
#: shell is up. What remains is the *data* round trip that paints the value — for the speaker
#: rename that is the speaker list the transcript labels resolve against.
#:
#: 30 s, raised from a bare ``15000`` literal, and the measurement is the reason. On
#: 2026-09-08 ``test_rename_via_transcript_editor_propagates_to_other_file`` failed the full
#: gate at exactly this wait, having passed every earlier wait in the same test including a
#: 25 s one on the same page. Run standalone against the same stack and commit it passed
#: **3/3 in 35-45 s each**. So the propagation was working; 15 s was simply the shortest
#: budget in a test whose siblings on the same page already allow 25 s.
#:
#: ⚠️ Widening this does NOT weaken the assertion. If the rename genuinely fails to reach
#: Postgres, the test still fails — 15 s later. What it stops is the gate reporting a
#: propagation bug when what it measured was contention: the e2e phase runs 3 Playwright
#: workers that each upload and really transcribe media, so the pipeline is saturated by the
#: suite itself. That is the same "the gate is the load" class as
#: ``backend/tests/CLAUDE.md``'s timeout table, and this is the third distinct test to be
#: bitten by it in three consecutive runs.
DATA_AFTER_RELOAD_MS = 30_000
