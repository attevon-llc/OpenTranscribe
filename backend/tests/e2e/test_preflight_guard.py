"""Self-test for the E2E stack preflight detector in ``conftest.py``.

The preflight exists to turn two environment failures into one legible message
instead of an arbitrary subset of per-test timeouts. That only holds if the
detector can actually FIRE — a scanner that silently matches nothing reports a
clean stack and a broken one identically, which is the failure mode
``scripts/audit-tests.py --selftest`` and ``npm run test:audit:selftest`` exist to
prevent elsewhere in this repo.

So every case below is either a **must-fire** or a **must-stay-clean** fixture,
built from synthetic module text. Nothing here touches the browser, the stack or
the network, so it runs in milliseconds and cannot be skipped into uselessness.
"""

import pytest
from conftest import split_store_modules

pytestmark = pytest.mark.e2e


#: The exact shape Vite's dev server emits, and the exact shape that broke:
#: TagManagerModal pinned to a stale stamp while the layout moved on.
_STALE = 'import { user } from "/src/stores/auth.ts?t=1786436180151";'
_CURRENT = 'import { user } from "/src/stores/auth.ts?t=1786539312580";'


class TestMustFire:
    """Cases the detector is required to catch."""

    def test_two_stamps_for_one_store_is_reported(self):
        splits = split_store_modules(
            {
                "src/routes/+layout.svelte": _CURRENT,
                "src/components/tags/TagManagerModal.svelte": _STALE,
            }
        )

        assert "auth" in splits, "a store served under two ?t= stamps must be reported"
        assert set(splits["auth"]) == {"1786436180151", "1786539312580"}

    def test_the_importers_are_named_so_the_message_is_actionable(self):
        splits = split_store_modules(
            {
                "src/routes/+layout.svelte": _CURRENT,
                "src/components/tags/TagManagerModal.svelte": _STALE,
            }
        )

        # Without the importer paths the operator learns that something split but
        # not which component is stale, which is the only fact that shortens the fix.
        assert splits["auth"]["1786436180151"] == ["src/components/tags/TagManagerModal.svelte"]
        assert splits["auth"]["1786539312580"] == ["src/routes/+layout.svelte"]

    def test_a_split_in_any_store_is_caught_not_just_auth(self):
        splits = split_store_modules(
            {
                "a.svelte": 'import { locale } from "/src/stores/locale.ts?t=111";',
                "b.svelte": 'import { locale } from "/src/stores/locale.ts?t=222";',
            }
        )

        assert set(splits) == {"locale"}

    def test_three_way_split_reports_every_stamp(self):
        splits = split_store_modules(
            {
                "a.svelte": 'from "/src/stores/auth.ts?t=1"',
                "b.svelte": 'from "/src/stores/auth.ts?t=2"',
                "c.svelte": 'from "/src/stores/auth.ts?t=3"',
            }
        )

        assert len(splits["auth"]) == 3


class TestMustStayClean:
    """Cases that must NOT be reported, or the guard becomes noise and gets muted."""

    def test_one_stamp_shared_by_every_importer_is_clean(self):
        assert (
            split_store_modules(
                {
                    "src/routes/+layout.svelte": _CURRENT,
                    "src/components/Navbar.svelte": _CURRENT,
                    "src/components/tags/TagManagerModal.svelte": _CURRENT,
                }
            )
            == {}
        )

    def test_different_stores_on_different_stamps_are_not_a_split(self):
        # Each store is internally consistent; only a store split ACROSS stamps
        # produces two live copies of the same state.
        assert (
            split_store_modules(
                {
                    "a.svelte": 'from "/src/stores/auth.ts?t=111"',
                    "b.svelte": 'from "/src/stores/locale.ts?t=222"',
                }
            )
            == {}
        )

    def test_a_bundled_frontend_has_no_stamps_and_is_clean(self):
        # The prod/nginx overlays serve a bundle: no ?t= URLs exist, the failure
        # mode is impossible, and the guard must be a no-op rather than a false alarm.
        assert split_store_modules({"index.html": "<!doctype html><html></html>"}) == {}

    def test_empty_input_is_clean(self):
        assert split_store_modules({}) == {}
