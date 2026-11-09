"""Typed per-page state for the e2e fixtures.

A fixture that hands state to the test receiving its page (captured console errors, routes
held open by the test, the lost-frames script state) cannot declare an attribute on the
Playwright ``Page``, which does not know about it. Each registry below keys one typed value by
the page object instead. Entries are weak, so they go when the page does.
"""

from __future__ import annotations

from weakref import WeakKeyDictionary

from playwright.sync_api import Page
from playwright.sync_api import Route


class PageState[T]:
    """One value of type ``T`` per Playwright page."""

    def __init__(self) -> None:
        self._values: WeakKeyDictionary[Page, T] = WeakKeyDictionary()

    def attach(self, page: Page, value: T) -> None:
        self._values[page] = value

    def of(self, page: Page) -> T:
        return self._values[page]


console_errors: PageState[list[str]] = PageState()
held_routes: PageState[list[Route]] = PageState()
card_names: PageState[list[str]] = PageState()
