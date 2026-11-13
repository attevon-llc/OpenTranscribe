"""Which frontend sources the e2e preflight may request from the Vite dev server."""

import re
from pathlib import Path

_TEST_ONLY_NAMES = ("test-setup.ts", "test-network-guard.ts")
_TEST_ONLY_SUFFIXES = (".test.ts", ".spec.ts", ".testHelpers.ts")


def is_test_only_module(path: Path, text: str) -> bool:
    """True for modules that only the vitest runner loads, never the browser.

    Requesting one from the Vite dev server makes Vite discover ``vitest`` as a new
    dependency, re-run dependency pre-bundling and reload every connected page, which
    aborts in-flight dynamic imports in the other xdist workers' browsers
    (https://vite.dev/guide/dep-pre-bundling, "Automatic Dependency Discovery").
    """
    return (
        path.name.endswith(_TEST_ONLY_SUFFIXES)
        or path.name in _TEST_ONLY_NAMES
        or "test-mocks" in path.parts
        or re.search(r"""from\s+['"]vitest['"]|import\(\s*['"]vitest['"]""", text) is not None
    )


def store_importer_candidates(frontend: Path) -> list[Path]:
    """Browser-graph source files under ``frontend/src`` that import a ``$stores/`` module."""
    found: list[Path] = []
    for pattern in ("src/**/*.svelte", "src/**/*.ts"):
        for path in frontend.glob(pattern):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "$stores/" in text and not is_test_only_module(path, text):
                found.append(path)
    return found
