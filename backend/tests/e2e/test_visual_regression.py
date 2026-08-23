"""E2E visual-regression screenshot baselines.

Pixel-level regression guard for the four primary surfaces touched by the
frontend component refactor (branch: refactor/frontend-overhaul), captured in
BOTH light and dark themes:

- gallery / home (``/``)
- file-detail / transcript (``/files/{uuid}``)
- speakers (``/speakers``)
- the Settings modal

pytest-playwright (Python) has no built-in ``to_have_screenshot`` (that's the
JS runner), so this implements a pragmatic approach: take a full-page
``page.screenshot`` at a FIXED viewport and compare against a committed baseline
PNG under ``__screenshots__/`` using a small numpy pixel diff (numpy + Pillow are
already project deps — no new dependency). A change fails only when the fraction
of differing pixels exceeds a small tolerance (anti-aliasing slack).

First run (or after an intentional UI change), write/refresh the baselines::

    UPDATE_SCREENSHOTS=1 pytest backend/tests/e2e/test_visual_regression.py -v

Then re-run WITHOUT the env var to compare against the committed baselines::

    pytest backend/tests/e2e/test_visual_regression.py -v

Requirements:
- An ISOLATED, seeded stack — never the shared live dev stack (issue #451):
  ``./opentr.sh start dev --fresh <name> --port-offset N --seed-benchmark``,
  then point ``--base-url``/``--backend-url`` (or ``base_url``/``backend_url``,
  see conftest) at its ports.
- At least one completed, transcribed file with segments in that dataset
  (``--seed-benchmark`` uploads a fixed, small set via
  ``scripts/seed-fresh-deployment.sh``; wait for all of them to leave
  "processing" before running this suite)
- admin@example.com / password (seeded automatically on a fresh deployment)

Why isolation is not optional here (issue #451): ``gallery`` and ``file_detail``
are full-page captures of live, mutable, newest-first content, and
``transcribed_file_uuid`` below deliberately selects the NEWEST completed file
with segments — the same query the app itself uses. Against the shared dev
stack, any upload from any other session repoints that query at a different
file and invalidates the baseline; the fix is not to make the query
order-independent (the app's own "newest first" behavior is exactly what
should be under test) but to run against a stack nothing else will ever touch.
On a single-purpose ``--fresh`` deployment, "newest" is as deterministic as a
hard-coded UUID, because nothing else can upload to it between two runs.
"""

from __future__ import annotations

import io
import os
import socket
import uuid as uuid_pkg
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

# Flat import, NOT `from tests.e2e._visual_diff import ...`.
#
# This module only ever runs under `tests/e2e/pytest.ini`, which makes
# `tests/e2e` the rootdir; `tests/` is not a package and never reaches sys.path
# there, so the dotted form raises `ModuleNotFoundError: No module named 'tests'`
# and the ENTIRE visual suite fails to collect. It shipped that way briefly and
# was invisible because the eight baselines were already failing for unrelated
# reasons, so nobody ran the module. `tests/unit/test_visual_diff_fraction.py`
# keeps the dotted form, which is correct under the repo-root rootdir it runs in.
from _visual_diff import CHANNEL_NOISE_THRESHOLD  # noqa: F401
from _visual_diff import DIFF_TOLERANCE
from _visual_diff import diff_fraction as _diff_fraction
from PIL import Image
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.visual  # run-e2e.sh runs visual tests serially (quiet stack)

# This module used to define its own ``FRONTEND_URL``/``BACKEND_URL`` constants here.
# A module constant is evaluated at import time, so it could not see ``--base-url`` /
# ``--backend-url`` and this file always drove whatever was on the default ports — even
# when the run was aimed at an isolated stack (issue #431). Everything below takes
# conftest's ``base_url`` / ``backend_url`` fixtures instead.
TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")  # noqa: S105

# Fixed viewport so baselines are deterministic across machines.
VIEWPORT = {"width": 1280, "height": 800}

SCREENSHOT_DIR = Path(__file__).parent / "__screenshots__"

# Set to "1" to (re)write baselines instead of comparing.
UPDATE_SCREENSHOTS = os.environ.get("UPDATE_SCREENSHOTS") == "1"


def _png_to_array(data: bytes) -> np.ndarray:
    """Decode PNG bytes into an RGB uint8 numpy array."""
    with Image.open(io.BytesIO(data)) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


def _compare_or_write(name: str, png_bytes: bytes) -> None:
    """Compare a screenshot against its baseline, or write it in update mode.

    Args:
        name: Baseline file stem (theme + surface), e.g. ``gallery-dark``.
        png_bytes: Full-page PNG screenshot bytes.
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path = SCREENSHOT_DIR / f"{name}.png"

    if UPDATE_SCREENSHOTS:
        baseline_path.write_bytes(png_bytes)
        return

    if not baseline_path.exists():
        # A missing baseline is a FAILURE, never an auto-write.
        #
        # This used to write the file and `pytest.skip`, which made the suite
        # self-approving in two runs with no human ever looking at the image:
        # run once to write, run again to "pass" against what the possibly
        # broken build just produced. `scripts/e2e/run-e2e.sh` treats skips as
        # success, so the first run was green too.
        pytest.fail(
            f"No baseline for '{name}' at {baseline_path}. A screenshot is only "
            f"a baseline once a human has looked at it. Generate it deliberately "
            f"with UPDATE_SCREENSHOTS=1 and review the image in the diff before "
            f"committing it."
        )

    current = _png_to_array(png_bytes)
    baseline = _png_to_array(baseline_path.read_bytes())

    # Shapes may differ; _diff_fraction charges the non-overlapping area as
    # differing rather than cropping it away unseen.
    fraction = _diff_fraction(current, baseline)
    if fraction > DIFF_TOLERANCE:
        # Persist the failing capture next to the baseline for inspection.
        actual_path = SCREENSHOT_DIR / f"{name}.actual.png"
        actual_path.write_bytes(png_bytes)
        pytest.fail(
            f"Visual regression on '{name}': {fraction:.2%} of pixels changed "
            f"(tolerance {DIFF_TOLERANCE:.2%}). Wrote {actual_path.name} for "
            f"inspection. If intentional, refresh with "
            f"UPDATE_SCREENSHOTS=1 pytest backend/tests/e2e/test_visual_regression.py"
        )


# ---------------------------------------------------------------------------
# Discover a transcribed file for the file-detail surface.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def api_token(backend_url: str) -> str:
    """Authenticate once per module via the backend API.

    Also dismisses the FirstRunWizard (``FirstRunWizard.svelte`` +
    ``POST /admin/first-run-wizard/complete``) for the super_admin account. It
    mounts unconditionally in the root layout and, on an account that has never
    completed it — true of a brand-new ``--fresh --seed-benchmark`` admin —
    opens a `BaseModal` on first authenticated page load whose backdrop
    intercepts every click, including the settings surface's `.user-button`.
    There is no localStorage/query-param gate to suppress it with; its
    visibility is entirely server-derived from `SystemSettings`, so the only
    way to keep it from appearing is to call the same completion endpoint the
    UI's own skip button calls. Idempotent — safe to call even if already
    completed.
    """
    resp = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        pytest.skip(f"Cannot authenticate against dev stack (HTTP {resp.status_code})")
    token = str(resp.json()["access_token"])
    requests.post(
        f"{backend_url}/api/admin/first-run-wizard/complete",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    return token


@pytest.fixture(scope="module")
def transcribed_file_uuid(api_token: str, backend_url: str) -> str:
    """Discover a completed file that has transcript segments (or skip)."""
    listing = requests.get(
        f"{backend_url}/api/files",
        headers={"Authorization": f"Bearer {api_token}"},
        params={"page": "1", "page_size": "100", "sort_by": "upload_time", "sort_order": "desc"},
        timeout=30,
    )
    items: list[dict[str, Any]] = listing.json().get("items", listing.json().get("files", []))
    for f in items:
        if f.get("status") != "completed":
            continue
        detail = requests.get(
            f"{backend_url}/api/files/{f['uuid']}",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30,
        ).json()
        if detail.get("transcript_segments"):
            return str(f["uuid"])
    pytest.skip("No completed transcribed file in dev dataset — required for file-detail capture")
    return ""  # unreachable, satisfies typing


#: HOST-side probe port — see the same constant in `test_chat_trace_panel.py`.
#: `--port-offset N` moves it, and a hardcoded 5199 silently SKIPS this surface
#: on an offset stack while appearing to pass.
MOCK_LLM_PORT = int(os.environ.get("MOCK_LLM_PORT", "5199"))
#: CONTAINER-side port, which the offset never moves.
MOCK_LLM_URL_FOR_BACKEND = "http://mock-llm:5199/v1"
ROOMY_CONTEXT_WINDOW = 32_000
TRACE_PANEL = '[data-testid="chat-trace-panel"]'
TRACE_NODE = '[data-testid="trace-node"]'
#: Asked identically by the warm-up turn and the captured turn — the retrieval
#: cache is keyed on user + org + query + scope + settings revision, so they
#: must match exactly or the capture is a miss.
TRACE_QUESTION = "Which risks did the speakers highlight?"


def _mock_llm_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", MOCK_LLM_PORT)) == 0


def _warm_retrieval_cache(backend_url: str, auth: dict[str, str], provider_uuid: str) -> None:
    """Ask ``TRACE_QUESTION`` once in a THROWAWAY conversation, then delete it.

    A throwaway one rather than the conversation about to be captured: a second
    turn in the same thread has history, so the query rewriter runs and rewrites
    the question — which changes the cache key and misses the very cache this
    call exists to populate, while also flipping the ``Rewritten`` row.

    Best-effort. A stack without Redis simply captures a cache miss, and the
    baseline written from it is still internally consistent; failing the whole
    visual suite over a warm-up would be worse than the noise it prevents.
    """
    conversation = requests.post(
        f"{backend_url}/api/chat/conversations",
        headers=auth,
        json={
            "llm_config_uuid": provider_uuid,
            "scope": {"file_uuids": [], "collection_uuids": [], "tag_names": [], "speakers": []},
            "settings": {"use_context": True},
        },
        timeout=30,
    )
    if not conversation.ok:
        return
    warmup_uuid = str(conversation.json()["uuid"])
    try:
        with requests.post(
            f"{backend_url}/api/chat/conversations/{warmup_uuid}/messages",
            headers={**auth, "Accept": "text/event-stream"},
            json={"content": TRACE_QUESTION},
            stream=True,
            timeout=180,
        ) as response:
            # Drain to completion: the cache is written when retrieval finishes,
            # and abandoning the stream early would race it.
            for _ in response.iter_lines(decode_unicode=True):
                pass
    except requests.RequestException:
        return
    finally:
        try:
            requests.delete(
                f"{backend_url}/api/chat/conversations/{warmup_uuid}", headers=auth, timeout=30
            )
        except requests.RequestException:
            pass


@pytest.fixture
def trace_conversation_uuid(api_token: str, backend_url: str) -> Iterator[str]:
    """A FRESH conversation pinned to the mock LLM, for the ``chat_trace`` surface.

    ⚠️ Function-scoped, and that is what makes the baselines reproducible. A
    module-scoped conversation was shared by both themes, so the light capture
    was turn 1 (no history, ``Rewritten`` reports SKIPPED) and the dark capture
    was turn 2 (history exists, the rewriter runs and reports DONE, and the
    rewritten query's length moves the excerpt budget). Both images were
    correct; neither could survive the themes running in a different order or
    in isolation. One conversation per capture makes every capture turn 1.

    ⚠️ It also WARMS THE RETRIEVAL CACHE before yielding, which is not an
    optimisation. Whether the captured turn is a cache hit or a miss changes
    four rows — ``Cache`` reads CACHED or EMPTY, and search/rerank/sample read
    SKIPPED or DONE — so leaving it to chance means the baseline depends on
    whether anything asked the same question inside ``cache_ttl_seconds``. The
    first run of a fresh stack would record a miss and every later run would
    fail against it. Warming it makes the hit deliberate and time-independent,
    and a hit is the more interesting image anyway: it is the state that shows
    skipped work being reported rather than omitted.

    Torn down in a ``finally`` — the assertion most likely to fail is the one
    AFTER these were created.
    """
    if not _mock_llm_running():
        pytest.skip("chat_trace needs the mock LLM: ./opentr.sh start dev --with-mock-llm")

    auth = {"Authorization": f"Bearer {api_token}"}
    provider = requests.post(
        f"{backend_url}/api/llm-settings",
        headers=auth,
        json={
            "name": f"gh514-visual-{uuid_pkg.uuid4().hex[:8]}",
            "provider": "custom",
            "model_name": "mock-gpt",
            "base_url": MOCK_LLM_URL_FOR_BACKEND,
            "api_key": "not-needed",
            "max_tokens": ROOMY_CONTEXT_WINDOW,
        },
        timeout=30,
    )
    assert provider.ok, f"Could not create provider: {provider.status_code} {provider.text}"
    provider_uuid = str(provider.json()["uuid"])

    conversation_uuid = ""
    try:
        conversation = requests.post(
            f"{backend_url}/api/chat/conversations",
            headers=auth,
            json={
                "llm_config_uuid": provider_uuid,
                "scope": {
                    "file_uuids": [],
                    "collection_uuids": [],
                    "tag_names": [],
                    "speakers": [],
                },
                "settings": {"use_context": True},
            },
            timeout=30,
        )
        assert conversation.ok, f"Could not create conversation: {conversation.status_code}"
        conversation_uuid = str(conversation.json()["uuid"])
        _warm_retrieval_cache(backend_url, auth, provider_uuid)
        yield conversation_uuid
    finally:
        for url in (
            f"{backend_url}/api/chat/conversations/{conversation_uuid}"
            if conversation_uuid
            else "",
            f"{backend_url}/api/llm-settings/config/{provider_uuid}",
        ):
            if not url:
                continue
            try:
                requests.delete(url, headers=auth, timeout=30)
            except requests.RequestException:
                pass


# ---------------------------------------------------------------------------
# Per-theme authenticated context. Theme is forced via localStorage in an init
# script BEFORE first paint (matches static/theme.js, which reads it on load).
# ---------------------------------------------------------------------------
def _login(page: Page, base_url: str) -> None:
    """Log in via the form, tolerating an already-authenticated context."""
    page.goto(base_url)
    if page.locator(".user-button").count():
        page.wait_for_selector(".user-button", timeout=10000)
        return
    page.wait_for_selector("#email", timeout=15000)
    page.fill("#email", TEST_ADMIN_EMAIL)
    page.fill("#password", TEST_ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector(".user-button", timeout=30000)


def _make_context(browser: Any, theme: str) -> Any:
    """Build a browser context pinned to a viewport and a forced theme."""
    context = browser.new_context(
        viewport=VIEWPORT,
        ignore_https_errors=True,
        # Freeze animations/transitions and CSS caret so screenshots are stable.
        reduced_motion="reduce",
    )
    context.add_init_script(f"window.localStorage.setItem('theme', '{theme}');")
    return context


def _stabilize(page: Page) -> None:
    """Quiet the page before capture: network idle, no animations, no caret."""
    page.wait_for_load_state("networkidle")
    # Disable animations/transitions and pause media to remove non-determinism.
    page.add_style_tag(
        content=(
            "*,*::before,*::after{animation:none!important;"
            "transition:none!important;caret-color:transparent!important}"
        )
    )
    page.evaluate(
        "document.querySelectorAll('video,audio').forEach(m=>{try{m.pause();"
        "m.currentTime=0;}catch(e){}})"
    )
    # Kept deliberately: a paint/layout settle before a screenshot. The comparison is a
    # pixel diff, not a locator, so there is nothing to auto-wait on (issue #431).
    page.wait_for_timeout(600)


# Surfaces parametrized over both themes. Each entry: (surface, theme).
SURFACES = ["gallery", "file_detail", "speakers", "settings", "chat_trace"]
THEMES = ["light", "dark"]


#: Regions whose pixels change without the UI changing, per surface (issue #451).
#:
#: These are masked out of the capture rather than tolerated by the diff budget.
#: Two back-to-back runs on identical code and data differ by 0.08–0.09% purely
#: because of these elements — live CPU/disk/GPU-VRAM gauges and a relative
#: "Last run: 21m ago" chip. That is inside the 0.5% tolerance today, so the suite
#: passes, but the headroom is only ~5.5x and it shrinks as digit widths change
#: over longer intervals. A tolerance absorbing known noise is a tolerance that
#: cannot also catch a small real regression.
#:
#: The counters are masked for a second and more important reason: they render
#: live dev-database totals (users, files, segments, clusters, profiles). A
#: baseline containing them is invalidated by the next upload, which is exactly
#: why the current 8 baselines cannot be honestly refreshed.
#:
#: ⚠️ Only selectors VERIFIED to match on the live app are listed. A first draft
#: also carried `settings`: `.stat-value`, `.stat-detail`, `.progress-fill`,
#: `.model-value` — all four matched **zero** elements, because the query was
#: made right after `.settings-modal` became visible, before its default
#: `system-statistics` section finished its own async `GET /system/stats` call
#: (`SystemStatisticsPanel.svelte`; `statsLoading` gates the whole grid, and the
#: GPU card can retry once more with its own 5s delay). The settings branch of
#: `test_visual_regression` now waits for `.stats-grid` itself before capturing,
#: which is what makes the entry below actually match something.
#:
#: `settings` masks the ENTIRE `.stats-grid`, not the individual `.stat-value` /
#: `.stat-detail` / `.progress-fill` classes inside it. Those classes are reused
#: across every card in the grid (users, files, tasks, throughput, queue depth,
#: model names, CPU/mem/disk/GPU) with no per-metric class, and one card
#: (Search Index) only renders once its own async status has loaded — so
#: `:nth-child` targeting is order-fragile in a way a single outer selector is
#: not. Every number in that grid is either live host telemetry (CPU/disk/GPU%)
#: or a DB total that is only stable because nothing else can write to this
#: isolated stack between two runs; masking the whole region is the box that
#: still lets the surrounding chrome (sidebar nav, modal frame, section header)
#: be compared for real.
#: Do not add a selector here without checking `page.locator(sel).count()`.
_VOLATILE_SELECTORS: dict[str, tuple[str, ...]] = {
    # "Last run: N minutes ago" (1 element) + per-cluster membership counts (20).
    "speakers": (".last-clustered-chip", ".member-count"),
    # Users/files/tasks/throughput/queue/model/CPU/mem/disk/GPU cards — live
    # telemetry and DB totals, all inside one wrapper (see comment above).
    "settings": (".settings-modal .stats-grid",),
    # Every trace node renders the real wall-clock cost of its stage (GH #514).
    # Those milliseconds differ on EVERY run by construction — that is the whole
    # point of measuring them — so a baseline containing them would be
    # un-reproducible from the very first commit. Masking them leaves the part
    # worth comparing: row order, marker shapes, labels, counts, and the
    # skipped-row de-emphasis.
    "chat_trace": (".trace-ms",),
}


def _run_traced_turn(page: Page, base_url: str, conversation_uuid: str) -> None:
    """Ask one question and leave the finished trace panel on screen.

    ⚠️ **Capture must happen in the same page session as the turn.** Traces are
    live-only by design, so a reload — the obvious way to reach a known state —
    replaces the tree with the "not stored" notice and the baseline would record
    an empty panel that looks perfectly plausible.

    The context is built with ``reduced_motion="reduce"``, which is what makes
    this deterministic rather than merely slow: it bypasses the reveal pacer
    entirely, so every node is on screen the moment its frame lands instead of
    cascading on a 55 ms ticker that the capture could race.
    """
    page.goto(f"{base_url}/chat/{conversation_uuid}")
    composer = page.locator('[data-testid="chat-composer-input"]')
    expect(composer).to_be_visible(timeout=30000)
    composer.fill(TRACE_QUESTION)
    page.locator('[data-testid="chat-send"]').click()
    # Completion is read from the assistant bubble, NOT from Send returning:
    # Send is visible before the click flips it to Stop, so that assertion would
    # pass against the pre-click state and capture a half-drawn tree.
    expect(
        page.locator('[data-testid="chat-message-assistant"][data-status="complete"]').last
    ).to_be_visible(timeout=90000)

    if page.locator(TRACE_PANEL).count() == 0:
        page.locator('[data-testid="chat-trace-toggle"]').click()
    expect(page.locator(TRACE_PANEL)).to_be_visible(timeout=15000)
    expect(page.locator(TRACE_NODE).first).to_be_visible(timeout=15000)
    _stabilize(page)


def _volatile_regions(page: Page, surface: str) -> list[Any]:
    """Locators to paint over before comparing, for *surface*.

    Only selectors that actually match are returned. Playwright masks every
    element a locator resolves to, and a selector matching nothing is silently a
    no-op — so a renamed class would quietly stop masking and reintroduce the
    noise it was added to remove. That is not hypothetical: the first draft of
    `_VOLATILE_SELECTORS` listed four settings selectors that matched **zero**
    elements, and nothing about the run said so.

    The masked surfaces therefore assert their own coverage below rather than
    trusting the table.
    """
    return [
        page.locator(selector)
        for selector in _VOLATILE_SELECTORS.get(surface, ())
        if page.locator(selector).count()
    ]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("surface", SURFACES)
def test_visual_regression(
    browser: Any,
    theme: str,
    surface: str,
    transcribed_file_uuid: str,
    base_url: str,
    request: pytest.FixtureRequest,
) -> None:
    """Capture and compare a full-page screenshot for each surface and theme."""
    context = _make_context(browser, theme)
    page = context.new_page()
    try:
        _login(page, base_url)
        # Confirm the forced theme actually took effect.
        applied = page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert applied == theme, f"Expected data-theme={theme}, got {applied}"

        if surface == "gallery":
            page.goto(base_url)
            page.wait_for_selector(".gallery-action-buttons", timeout=30000)
            page.wait_for_selector(".file-card, .file-list-row", timeout=30000)
            _stabilize(page)
        elif surface == "file_detail":
            page.goto(f"{base_url}/files/{transcribed_file_uuid}")
            page.wait_for_selector(".transcript-segment", timeout=30000)
            _stabilize(page)
        elif surface == "speakers":
            page.goto(f"{base_url}/speakers")
            page.wait_for_selector(".speakers-page", timeout=30000)
            _stabilize(page)
            # A masked surface must actually mask something. Playwright treats a
            # selector matching nothing as a silent no-op, so without this the
            # relative-time chip and the live cluster counts would drift back
            # into the baseline the moment a class is renamed — and the run would
            # look identical. Asserted here rather than in a separate test
            # because it is only knowable against the rendered page.
            assert _volatile_regions(page, "speakers"), (
                "None of _VOLATILE_SELECTORS['speakers'] matched anything on the "
                "speakers page, so this capture masks nothing and its baseline "
                "will absorb the 'Last run: N ago' chip and live cluster counts."
            )
        elif surface == "settings":
            page.goto(base_url)
            page.wait_for_selector(".user-button", timeout=30000)
            page.locator(".user-button").click()
            settings_item = page.locator(".dropdown-menu .dropdown-item", has_text="Settings")
            expect(settings_item.first).to_be_visible(timeout=5000)
            settings_item.first.click()
            expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)
            # The modal opens directly into the "System Statistics" section
            # (Navbar.svelte + settingsModalStore's default), but that section's
            # numbers arrive from its own async GET /system/stats — waiting on
            # `.settings-modal` alone races that call and is why an earlier draft
            # of _VOLATILE_SELECTORS found nothing to mask.
            page.wait_for_selector(".settings-modal .stats-grid", timeout=15000)
            _stabilize(page)
            # See the speakers branch above for why this assertion exists: a
            # masked surface must actually mask something, or a class rename
            # silently lets the live gauges/DB totals back into the baseline.
            assert _volatile_regions(page, "settings"), (
                "_VOLATILE_SELECTORS['settings'] matched nothing on the settings "
                "modal, so this capture masks nothing and its baseline will "
                "absorb live CPU/disk/GPU gauges and DB totals."
            )
        elif surface == "chat_trace":
            # Resolved lazily, NOT as a test parameter: the fixture skips without
            # the mock LLM, and a declared parameter would take the other four
            # surfaces down with it on a stack that simply has no LLM running.
            _run_traced_turn(page, base_url, request.getfixturevalue("trace_conversation_uuid"))
            # See the speakers branch: a masked surface must actually mask
            # something. Every row's `ms` is genuinely different each run, so an
            # unmatched selector here does not merely add noise — it guarantees
            # the baseline can never pass a second time.
            assert _volatile_regions(page, "chat_trace"), (
                "_VOLATILE_SELECTORS['chat_trace'] matched nothing, so this capture "
                "bakes live per-stage timings into the baseline and can never match again."
            )
            # The warm-up is only useful if it took effect. A miss here still
            # produces a plausible-looking image, and the image is the only
            # thing a reviewer sees — so the precondition is asserted rather
            # than hoped for.
            cached = page.locator(f'{TRACE_NODE}[data-outcome="cached"]')
            assert cached.count() == 1, (
                "expected exactly one cached node — the fixture's warm-up turn did not "
                f"populate the retrieval cache (saw {cached.count()}), so this baseline "
                "records a cache MISS and will not reproduce once the cache is warm."
            )
        else:  # pragma: no cover - defensive
            pytest.fail(f"Unknown surface: {surface}")

        if surface == "chat_trace":
            # Capture the PANEL, not the page. The thread beside it carries the
            # model's answer and a relative timestamp, neither of which this
            # baseline is about; an element capture makes the image exactly the
            # feature under test.
            png_bytes = page.locator(TRACE_PANEL).screenshot(
                animations="disabled",
                mask=_volatile_regions(page, surface),
                mask_color="#ff00ff",
            )
        else:
            # The settings modal is an overlay; capture the viewport (not full page)
            # so a long scrolled background doesn't add nondeterministic height.
            full_page = surface != "settings"
            png_bytes = page.screenshot(
                full_page=full_page,
                animations="disabled",
                mask=_volatile_regions(page, surface),
                mask_color="#ff00ff",
            )
        _compare_or_write(f"{surface}-{theme}", png_bytes)
    finally:
        page.close()
        context.close()
