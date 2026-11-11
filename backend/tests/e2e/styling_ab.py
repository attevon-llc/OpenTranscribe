#!/usr/bin/env python3
"""Before/after screenshot and computed-style comparison for the frontend styling layer.

Two frontends (BEFORE and AFTER) are driven through the same screens against ONE backend, so
the data is identical. For every screen x variant (``en-light en-dark ar-light ar-dark``) the
tool takes a full-page PNG from each side (interleaved, A then B) and a computed-style
snapshot of every visible element, then reports:

* the fraction of pixels that differ beyond ``_visual_diff.CHANNEL_NOISE_THRESHOLD``;
* every element/property whose computed style differs.

A "zero" claim is falsifiable only through the computed-style diff: a 2 px padding change on
one button is far below the pixel tolerance of a full page. Run it through
``scripts/e2e/styling-ab.sh`` (builds the two trees and the two dev servers); see
``docs/design/frontend_styling_layer_plan.md`` section 6 and ``backend/tests/e2e/README.md``.

Modes
-----
``zero``      pass iff pixel diff <= 0.01% AND 0 computed-style differences, on every screen.
``intended``  differences are reported, not failed (``changes.txt`` is the list a reviewer
              checks against the lane card); screens named by ``--control`` must pass ``zero``.

Not named ``test_*``, so pytest does not collect it. Playwright is imported inside the
capture functions so the pure parts are unit-testable on a host without a browser.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from _visual_diff import CHANNEL_NOISE_THRESHOLD
from _visual_diff import diff_fraction
from PIL import Image
from styling_screens import DEFAULT_CONTROLS
from styling_screens import GLOBAL_MASKS
from styling_screens import SCREENS
from styling_screens import TRANSIENT_HIDE_CSS
from styling_screens import VARIANTS
from styling_screens import Screen

#: The acceptance threshold for ``zero`` mode (plan 6.4). Never widened.
ZERO_PIXEL_FRACTION = 0.0001

#: Injected on the AFTER side by ``--negative-control``. ``!important`` so it beats the scoped
#: ``.btn.svelte-xxx`` rules some components carry; the property names it moves are what
#: ``changes.txt`` must list.
NEGATIVE_CONTROL_CSS = ".btn { padding-inline: calc(var(--space-4, 1rem) + 1px) !important; }"
NEGATIVE_CONTROL_PROPS = ("padding-left", "padding-right")

SETTINGS_ATTR_HINT = (
    "SettingsModal has no data-section attribute on .nav-item. Add data-section={item.id} to "
    "the nav button in frontend/src/components/SettingsModal.svelte (one attribute, no "
    "visual change); see docs/design/frontend_styling_layer_plan.md section 6.2."
)

# The properties recorded per element (plan 6.2), as CSS longhands.
SNAPSHOT_PROPS = (
    "color",
    "background-color",
    "border-top-color",
    "border-right-color",
    "border-bottom-color",
    "border-left-color",
    "border-top-width",
    "border-right-width",
    "border-bottom-width",
    "border-left-width",
    "outline-color",
    "box-shadow",
    "border-top-left-radius",
    "border-top-right-radius",
    "border-bottom-right-radius",
    "border-bottom-left-radius",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "row-gap",
    "column-gap",
    "font-size",
    "font-weight",
    "line-height",
    "letter-spacing",
    "z-index",
    "opacity",
    "transition-duration",
    "top",
    "right",
    "bottom",
    "left",
    "width",
    "height",
)

_SNAPSHOT_JS = """
([masks, props]) => {
  const maskEls = [];
  for (const s of masks) {
    try { maskEls.push(...document.querySelectorAll(s)); } catch (e) { /* bad selector */ }
  }
  const skipTags = new Set(['SCRIPT', 'STYLE', 'LINK', 'META', 'NOSCRIPT', 'TEMPLATE']);
  const inMask = (el) => maskEls.some((m) => m === el || m.contains(el));
  const path = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      const parent = el.parentElement;
      let i = 1;
      if (parent) {
        for (const sib of parent.children) {
          if (sib === el) break;
          if (sib.tagName === el.tagName) i++;
        }
      }
      parts.push(el.tagName.toLowerCase() + ':' + i);
      el = parent;
    }
    return parts.reverse().join('>');
  };
  const read = (cs, prefix, into) => {
    for (const p of props) into[prefix + p] = cs.getPropertyValue(p);
  };
  const out = {};
  for (const el of document.body.querySelectorAll('*')) {
    if (skipTags.has(el.tagName)) continue;
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    if (inMask(el)) continue;
    const rec = {};
    read(getComputedStyle(el), '', rec);
    for (const pseudo of ['::before', '::after']) {
      const cs = getComputedStyle(el, pseudo);
      const content = cs.getPropertyValue('content');
      if (content && content !== 'none' && content !== 'normal') read(cs, pseudo + ' ', rec);
    }
    out[path(el)] = rec;
  }
  return out;
}
"""


# ---------------------------------------------------------------------------
# Pure parts (unit-tested in tests/unit/test_styling_ab.py)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Change:
    """One element/property whose computed value differs between the two sides."""

    key: str
    prop: str
    before: str
    after: str


def diff_snapshots(
    before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]
) -> list[Change]:
    """Return every computed-style difference between two snapshots.

    An element present on one side only is a change of the pseudo-property ``<element>``
    (``present`` / ``absent``), so a node that vanished cannot hide inside "0 differences".
    """
    changes: list[Change] = []
    for key in sorted(before.keys() | after.keys()):
        b, a = before.get(key), after.get(key)
        if b is None or a is None:
            changes.append(
                Change(
                    key,
                    "<element>",
                    "present" if b is not None else "absent",
                    "present" if a is not None else "absent",
                )
            )
            continue
        for prop in sorted(b.keys() | a.keys()):
            bv, av = b.get(prop, "<unset>"), a.get(prop, "<unset>")
            if bv != av:
                changes.append(Change(key, prop, bv, av))
    return changes


def aggregate_changes(per_screen: dict[str, list[Change]]) -> list[str]:
    """Group changes as ``prop: before -> after  (N elements; screens a, b)``.

    ``per_screen`` is keyed ``<screen>__<variant>``. This aggregate is what goes into a
    lane's commit body as the explicit list of visual changes.
    """
    groups: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "screens": set()}
    )
    for screen_key, changes in per_screen.items():
        for c in changes:
            g = groups[(c.prop, c.before, c.after)]
            g["n"] += 1
            g["screens"].add(screen_key)
    lines = []
    for (prop, before, after), g in sorted(groups.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        screens = ", ".join(sorted(g["screens"]))
        lines.append(f"{prop}: {before} -> {after}  ({g['n']} elements; screens {screens})")
    return lines


def verdict(
    mode: str,
    pixel_fraction: float,
    n_computed: int,
    is_control: bool = False,
    error: str | None = None,
    skipped: str | None = None,
) -> str:
    """Return ``PASS`` / ``FAIL`` / ``CHANGED`` / ``ERROR`` / ``SKIP`` for one screen x variant."""
    if skipped is not None:
        return "SKIP"
    if error is not None:
        return "ERROR"
    identical = pixel_fraction <= ZERO_PIXEL_FRACTION and n_computed == 0
    if mode == "zero" or is_control:
        return "PASS" if identical else "FAIL"
    return "PASS" if identical else "CHANGED"


def make_diff_image(before: np.ndarray, after: np.ndarray) -> np.ndarray:
    """The AFTER image in greyscale with differing pixels painted magenta.

    Differing means beyond ``CHANNEL_NOISE_THRESHOLD`` on any channel; where the sizes differ
    every non-overlapping pixel is painted too (same rule as ``diff_fraction``).
    """
    h, w = max(before.shape[0], after.shape[0]), max(before.shape[1], after.shape[1])
    canvas_b = np.full((h, w, 3), 255, dtype=np.int16)
    canvas_a = np.full((h, w, 3), 255, dtype=np.int16)
    canvas_b[: before.shape[0], : before.shape[1]] = before
    canvas_a[: after.shape[0], : after.shape[1]] = after
    differing = np.any(np.abs(canvas_a - canvas_b) > CHANNEL_NOISE_THRESHOLD, axis=-1)
    covered = np.zeros((h, w), dtype=bool)
    mh, mw = min(before.shape[0], after.shape[0]), min(before.shape[1], after.shape[1])
    covered[:mh, :mw] = True
    differing |= ~covered
    grey = canvas_a.mean(axis=-1).astype(np.uint8)
    out = np.stack([grey, grey, grey], axis=-1)
    out[differing] = (255, 0, 255)
    return out


def select_screens(spec: str, screens: tuple[Screen, ...] = SCREENS) -> list[Screen]:
    """Resolve ``all`` / comma-separated ids / ``fnmatch`` globs (``settings-*``)."""
    if spec.strip() == "all":
        return list(screens)
    wanted = [p.strip() for p in spec.split(",") if p.strip()]
    chosen: list[Screen] = []
    for pattern in wanted:
        matched = [s for s in screens if fnmatch.fnmatchcase(s.id, pattern)]
        if not matched:
            raise SystemExit(f"--screens: {pattern!r} matches no screen id")
        chosen.extend(s for s in matched if s not in chosen)
    return chosen


def parse_variant(variant: str) -> tuple[str, str]:
    """``en-dark`` -> ``("en", "dark")``."""
    if variant not in VARIANTS:
        raise SystemExit(f"unknown variant {variant!r}; known: {', '.join(VARIANTS)}")
    locale, theme = variant.split("-", 1)
    return locale, theme


def negative_control_verdict(rows: list[dict[str, Any]]) -> tuple[bool, str]:
    """Did the injected change get caught everywhere it should have been?

    Every row whose AFTER page has a ``.btn`` must be a ``FAIL`` and its changes must name
    ``padding-left`` and ``padding-right``. Returns ``(ok, one-line summary)``.
    """
    with_btn = [r for r in rows if r.get("has_btn") and not r.get("error") and not r.get("skip")]
    if not with_btn:
        return False, "NEGATIVE CONTROL BROKEN: no captured screen contains a .btn"
    missed = [r["key"] for r in with_btn if r["verdict"] != "FAIL"]
    no_props = [
        r["key"]
        for r in with_btn
        if not all(p in r.get("changed_props", []) for p in NEGATIVE_CONTROL_PROPS)
    ]
    if missed or no_props:
        bad = sorted(set(missed) | set(no_props))
        return False, (
            f"NEGATIVE CONTROL BROKEN: {len(bad)}/{len(with_btn)} screens with a .btn did not "
            f"fail on the injected padding change: {', '.join(bad[:8])}"
        )
    return True, (
        f"NEGATIVE CONTROL OK: {len(with_btn)}/{len(with_btn)} screens with a .btn failed zero "
        f"and listed {'/'.join(NEGATIVE_CONTROL_PROPS)}"
    )


def split_step(step: str) -> tuple[str, str]:
    """``click:.x`` -> ``("click", ".x")``. Only the first colon splits (selectors have more)."""
    kind, _, arg = step.partition(":")
    return kind, arg


# ---------------------------------------------------------------------------
# Browser parts
# ---------------------------------------------------------------------------
class StepError(RuntimeError):
    """A step could not run; the screen is reported ERROR with this message."""


class ScreenUnavailableError(RuntimeError):
    """The screen cannot be shown on this stack (capability-gated); reported SKIP."""


def _open_settings(page: Any) -> None:
    if page.locator(".settings-modal").count():
        return
    page.locator(".user-button").click()
    # The first <button> in the menu is Settings (the rest are links / later buttons); this
    # is locale-independent, unlike matching the translated label.
    page.locator(".dropdown-menu button.dropdown-item").first.click()
    page.wait_for_selector(".settings-modal", timeout=15000)
    page.wait_for_selector(".settings-sidebar .nav-item", timeout=15000)


SAMPLE_WAV_NAME = "styling-sample.wav"


def sample_wav() -> bytes:
    """A fixed 0.1 s mono 8 kHz silent WAV: the same bytes (so the same hash) on every run."""
    import struct

    frames = 800
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16) + b"data"
    return header + struct.pack("<I", len(data)) + data


def _mock_route(page: Any, glob: str, status: int) -> None:
    """Answer ``glob`` with a fixed JSON error (``status`` > 0), or never (``status`` 0).

    Never answering holds the request open, which freezes a client in its in-flight state
    (an upload on "preparing") so the photograph does not depend on server timing.
    """
    if status == 0:
        page.route(glob, lambda route: None)
        return
    page.route(
        glob,
        lambda route: route.fulfill(
            status=status,
            content_type="application/json",
            body='{"detail": "Simulated upload failure"}',
        ),
    )


def run_step(page: Any, step: str) -> None:
    """Execute one manifest step (vocabulary in ``styling_screens``)."""
    kind, arg = split_step(step)
    try:
        if kind == "click":
            page.locator(arg).first.click()
        elif kind == "hover":
            page.locator(arg).first.hover()
        elif kind == "focus":
            loc = page.locator(arg).first
            loc.focus()
            # focus() alone does not always satisfy :focus-visible; a Tab / Shift+Tab round
            # trip returns here as a keyboard focus.
            page.keyboard.press("Tab")
            page.keyboard.press("Shift+Tab")
        elif kind == "wait":
            page.wait_for_selector(arg, timeout=30000)
        elif kind == "press":
            page.keyboard.press(arg)
        elif kind == "scroll":
            page.locator(arg).first.scroll_into_view_if_needed()
        elif kind == "settings":
            _open_settings(page)
            if page.locator(".settings-sidebar .nav-item[data-section]").count() == 0:
                raise StepError(SETTINGS_ATTR_HINT)
            item = page.locator(f'.settings-sidebar .nav-item[data-section="{arg}"]')
            if item.count() == 0 or item.first.is_disabled():
                raise ScreenUnavailableError(
                    f"settings section {arg!r} is not available for this user/stack"
                )
            item.first.click()
            page.wait_for_selector(
                f'.settings-sidebar .nav-item[data-section="{arg}"].active', timeout=15000
            )
        elif kind == "tab":
            page.locator(f"#tab-{arg}").first.click()
        elif kind == "local":
            key, _, value = arg.partition("=")
            page.evaluate("([k, v]) => localStorage.setItem(k, v)", [key, value])
        elif kind == "file":
            page.locator(arg).first.set_input_files(
                {"name": SAMPLE_WAV_NAME, "mimeType": "audio/wav", "buffer": sample_wav()}
            )
        elif kind == "route":
            glob, _, status = arg.rpartition("|")
            _mock_route(page, glob, int(status))
        elif kind == "reload":
            page.reload()
        elif kind == "pause":
            page.wait_for_timeout(int(arg))
        else:
            raise StepError(f"unknown step kind in {step!r}")
    except (StepError, ScreenUnavailableError):
        raise
    except Exception as exc:
        raise StepError(f"step {step!r} failed: {str(exc).splitlines()[0][:200]}") from exc


def _png_to_array(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


def _save_png(path: Path, arr: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path)


class Session:
    """Browser contexts for one side (before/after), cached per (variant, authed)."""

    def __init__(
        self, browser: Any, base: str, label: str, creds: tuple[str, str], inject_css: str | None
    ) -> None:
        self.browser, self.base, self.label = browser, base.rstrip("/"), label
        self.creds, self.inject_css = creds, inject_css
        self._ctx: dict[tuple[str, bool], Any] = {}

    def context(self, variant: str, authed: bool, fresh: bool = False) -> Any:
        """A context for this variant; logged in once if ``authed``.

        ``fresh`` returns a new uncached one: the caller closes it, so state the screen leaves
        behind (a persisted upload queue) cannot reach the next screen.
        """
        key = (variant, authed)
        if fresh:
            return self._new_context(variant, authed)
        if key not in self._ctx:
            self._ctx[key] = self._new_context(variant, authed)
        return self._ctx[key]

    def _new_context(self, variant: str, authed: bool) -> Any:
        from _visual_capture import login
        from _visual_capture import make_context

        locale, theme = parse_variant(variant)
        ctx = make_context(self.browser, theme, locale)
        if authed:
            page = ctx.new_page()
            login(page, self.base, *self.creds)
            page.close()
        return ctx

    def close(self) -> None:
        for ctx in self._ctx.values():
            ctx.close()


def capture(
    session: Session, screen: Screen, variant: str, file_uuid: str | None
) -> dict[str, Any]:
    """Photograph one screen on one side. Returns png bytes, snapshot and a .btn probe."""
    from _visual_capture import stabilize

    path = screen.path
    if "{file_uuid}" in path:
        if not file_uuid:
            raise ScreenUnavailableError(
                "no completed file on this stack (seed it: --seed-benchmark)"
            )
        path = path.replace("{file_uuid}", file_uuid)
    ctx = session.context(variant, screen.auth, fresh=screen.isolated)
    page = ctx.new_page()
    try:
        page.goto(session.base + path)
        if screen.ready:
            page.wait_for_selector(screen.ready, timeout=30000, state="attached")
        if session.inject_css:
            page.add_style_tag(content=session.inject_css)
        for step in screen.steps:
            run_step(page, step)
        if not any(step.startswith(("hover:", "focus:")) for step in screen.steps):
            # A click step leaves the pointer over its target, so the hover colour of whatever
            # sits there differs run to run depending on timing.
            page.mouse.move(0, 0)
        page.add_style_tag(content=TRANSIENT_HIDE_CSS)
        stabilize(page, network_idle=screen.network_idle)
        # The settings sidebar list scrolls the active row into view; how far it ended up
        # depends on layout timing (rows above it fill in as counts arrive), not on styling.
        # Park it at the top on both sides so the photograph is deterministic.
        page.evaluate(
            "document.querySelectorAll('.sidebar-scroll').forEach(e => { e.scrollTop = 0; })"
        )
        masks = [*GLOBAL_MASKS, *screen.masks]
        png = page.screenshot(
            full_page=screen.full_page,
            mask=[page.locator(m) for m in masks],
            mask_color="#ff00ff",
        )
        snapshot = page.evaluate(_SNAPSHOT_JS, [masks, list(SNAPSHOT_PROPS)])
        has_btn = bool(page.evaluate("() => !!document.querySelector('.btn')"))
        return {"png": png, "snapshot": snapshot, "has_btn": has_btn}
    finally:
        page.close()
        if screen.isolated:
            ctx.close()


def _api_token_and_file(backend: str, creds: tuple[str, str]) -> tuple[str, str | None]:
    """Log in over the API, dismiss the first-run wizard, find the newest completed file."""
    import requests

    resp = requests.post(
        f"{backend}/api/auth/token",
        data={"username": creds[0], "password": creds[1]},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(f"cannot authenticate against {backend} (HTTP {resp.status_code})")
    token = str(resp.json()["access_token"])
    headers = {"Authorization": f"Bearer {token}"}
    # The wizard mounts on first authenticated load of a fresh admin and its backdrop blocks
    # every click (same call test_visual_regression.py's api_token fixture makes). Idempotent.
    requests.post(f"{backend}/api/admin/first-run-wizard/complete", headers=headers, timeout=10)
    listing = requests.get(f"{backend}/api/files", headers=headers, timeout=30)
    items: Any = listing.json() if listing.ok else []
    if isinstance(items, dict):
        items = items.get("items") or items.get("files") or []
    for item in items:
        if str(item.get("status", "")).lower() == "completed" and item.get("uuid"):
            return token, str(item["uuid"])
    return token, None


def _warm(browser: Any, base: str) -> None:
    """Compile the dev server's module graph before the first capture.

    Vite transforms on demand, so the first page load of a cold server can exceed every
    selector timeout below (the same cold-start failure ``frontend_warm.py`` documents).
    """
    from _visual_capture import make_context

    ctx = make_context(browser, "light", "en")
    page = ctx.new_page()
    try:
        for path in ("/login", "/"):
            page.goto(base.rstrip("/") + path)
            page.wait_for_load_state("networkidle", timeout=180000)
    finally:
        page.close()
        ctx.close()


MAX_ATTEMPTS = 3


def measure(
    side_a: Session,
    side_b: Session,
    screen: Screen,
    variant: str,
    file_uuid: str | None,
    args: argparse.Namespace,
    controls: set[str],
) -> tuple[np.ndarray, np.ndarray, float, list[Change], dict[str, Any], int]:
    """Capture both sides; in zero mode re-capture a failing screen before calling it FAIL.

    A difference caused by a real style change shows on every attempt, while a timing flake
    (a transient overlay, a late repaint) does not, so only a screen that fails all
    ``MAX_ATTEMPTS`` attempts is reported as FAIL (a capture that errors is retried the same way). Negative controls are never retried.
    """
    retries = MAX_ATTEMPTS if args.mode == "zero" and not args.negative_control else 1
    for attempt in range(1, retries + 1):
        try:
            a = capture(side_a, screen, variant, file_uuid)
            b = capture(side_b, screen, variant, file_uuid)
        except ScreenUnavailableError:
            raise
        except Exception:
            # A page that never became ready under load is the same flake as a diff.
            if attempt == retries:
                raise
            continue
        arr_a, arr_b = _png_to_array(a["png"]), _png_to_array(b["png"])
        fraction = diff_fraction(arr_b, arr_a)
        changes = diff_snapshots(a["snapshot"], b["snapshot"])
        if verdict(args.mode, fraction, len(changes), is_control=screen.id in controls) != "FAIL":
            break
    return arr_a, arr_b, fraction, changes, b, attempt


def run(args: argparse.Namespace) -> int:
    """Capture every selected screen x variant on both sides and write the report."""
    from playwright.sync_api import sync_playwright

    out = Path(args.out).expanduser()
    creds = (
        os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com"),
        os.environ.get("E2E_ADMIN_PASSWORD") or "password",
    )
    screens = select_screens(args.screens)
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    for v in variants:
        parse_variant(v)
    controls = {c.strip() for c in args.control.split(",") if c.strip()}
    inject = NEGATIVE_CONTROL_CSS if args.negative_control else None
    if args.selfcheck and args.before.rstrip("/") != args.after.rstrip("/"):
        raise SystemExit("--selfcheck compares a server with itself: --before must equal --after")

    _, file_uuid = _api_token_and_file(args.backend.rstrip("/"), creds)
    rows: list[dict[str, Any]] = []
    per_screen_changes: dict[str, list[Change]] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for base in dict.fromkeys((args.before, args.after)):
            _warm(browser, base)
        side_a = Session(browser, args.before, "before", creds, None)
        side_b = Session(browser, args.after, "after", creds, inject)
        try:
            for screen in screens:
                for variant in variants:
                    key = f"{screen.id}__{variant}"
                    row: dict[str, Any] = {"key": key, "screen": screen.id, "variant": variant}
                    rows.append(row)
                    if screen.skip_reason:
                        row.update(skip=screen.skip_reason, verdict="SKIP")
                        print(f"  {key}: SKIP ({screen.skip_reason})", flush=True)
                        continue
                    try:
                        measured = measure(
                            side_a, side_b, screen, variant, file_uuid, args, controls
                        )
                    except ScreenUnavailableError as exc:
                        row.update(skip=str(exc), verdict="SKIP")
                        continue
                    except Exception as exc:
                        row.update(error=str(exc).splitlines()[0][:300] if str(exc) else repr(exc))
                        row["verdict"] = verdict(args.mode, 0, 0, error=row["error"])
                        print(f"  {key}: ERROR {row['error']}", flush=True)
                        continue
                    arr_a, arr_b, fraction, changes, b, attempts = measured
                    per_screen_changes[key] = changes
                    _save_png(out / "before" / f"{key}.png", arr_a)
                    _save_png(out / "after" / f"{key}.png", arr_b)
                    _save_png(out / "diff" / f"{key}.png", make_diff_image(arr_a, arr_b))
                    row.update(
                        pixel_diff=fraction,
                        computed_diffs=len(changes),
                        has_btn=b["has_btn"],
                        changed_props=sorted({c.prop for c in changes}),
                        attempts=attempts,
                        verdict=verdict(
                            args.mode, fraction, len(changes), is_control=screen.id in controls
                        ),
                    )
                    if attempts > 1 and row["verdict"] != "FAIL":
                        row["flaky"] = True
                    flaky = f" (flaky, passed on attempt {attempts})" if row.get("flaky") else ""
                    print(f"  {key}: {row['verdict']}{flaky}", flush=True)
        finally:
            side_a.close()
            side_b.close()
            browser.close()

    return report(args, out, rows, per_screen_changes)


def report(
    args: argparse.Namespace,
    out: Path,
    rows: list[dict[str, Any]],
    per_screen_changes: dict[str, list[Change]],
) -> int:
    """Write summary.json / changes.txt, print the table, return the exit code."""
    out.mkdir(parents=True, exist_ok=True)
    changes_lines = aggregate_changes(per_screen_changes)
    (out / "changes.txt").write_text("\n".join(changes_lines) + ("\n" if changes_lines else ""))
    detail = {
        k: [asdict(c) for c in v][:500] for k, v in per_screen_changes.items() if v
    }  # first 500 per screen keeps the file readable; changes.txt has the aggregate
    (out / "summary.json").write_text(
        json.dumps({"mode": args.mode, "rows": rows, "changes": detail}, indent=2)
    )

    print(f"\n{'screen':34} {'variant':9} {'pixel %':>8} {'computed':>9}  verdict")
    for r in rows:
        px = f"{r['pixel_diff'] * 100:8.4f}" if "pixel_diff" in r else f"{'-':>8}"
        cd = f"{r['computed_diffs']:9d}" if "computed_diffs" in r else f"{'-':>9}"
        note = r.get("error") or r.get("skip") or ""
        print(f"{r['screen']:34} {r['variant']:9} {px} {cd}  {r['verdict']} {note[:70]}")

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["verdict"]] += 1
    summary = ", ".join(f"{n} {v}" for v, n in sorted(counts.items()))
    print(f"\nSUMMARY ({args.mode}): {summary}; output {out}")

    if args.negative_control:
        ok, line = negative_control_verdict(rows)
        print(line)
        return 0 if ok else 1
    # Skips never fail a run; a screen that could not be captured or differs does.
    failing = counts.get("FAIL", 0) + counts.get("ERROR", 0)
    return 1 if failing else 0


def build_parser() -> argparse.ArgumentParser:
    """The CLI. ``scripts/e2e/styling-ab.sh`` is the normal entry point."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--before", required=True, help="BEFORE frontend URL")
    p.add_argument("--after", required=True, help="AFTER frontend URL")
    p.add_argument("--backend", required=True, help="the one shared backend URL")
    p.add_argument("--out", required=True, help="output directory (not /tmp: it is cleaned)")
    p.add_argument("--mode", choices=("zero", "intended"), default="zero")
    p.add_argument("--screens", default="all", help="all | ids | globs, comma separated")
    p.add_argument("--variants", default=",".join(VARIANTS))
    p.add_argument(
        "--control",
        default=",".join(DEFAULT_CONTROLS),
        help="screens that must pass zero even in intended mode",
    )
    p.add_argument(
        "--selfcheck",
        action="store_true",
        help="before == after: measures the noise floor; must pass zero",
    )
    p.add_argument(
        "--negative-control",
        action="store_true",
        help="inject a 1px .btn padding change on AFTER; must be caught",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
