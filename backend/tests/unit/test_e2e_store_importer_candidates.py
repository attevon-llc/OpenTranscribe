"""The e2e preflight must not request vitest-importing modules from the Vite dev server.

Doing so made Vite pre-bundle ``vitest`` mid-run and reload every page (v070verify gate).
"""

from pathlib import Path

from tests.e2e.store_importers import store_importer_candidates


def _write(root: Path, rel: str, text: str) -> None:
    path = root / "src" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_only_browser_graph_modules_are_candidates(tmp_path: Path) -> None:
    _write(tmp_path, "lib/y.ts", "import { s } from '$stores/auth';\n")
    _write(tmp_path, "components/A.svelte", "<script>import { s } from '$stores/auth';</script>\n")
    _write(tmp_path, "test-mocks/x.ts", "import { vi } from 'vitest';\nimport '$stores/auth';\n")
    _write(tmp_path, "lib/mockish.ts", "import { vi } from 'vitest';\nimport '$stores/auth';\n")
    _write(tmp_path, "lib/a.test.ts", "import '$stores/auth';\n")
    _write(tmp_path, "test-setup.ts", "import '$stores/auth';\n")
    _write(tmp_path, "lib/z.testHelpers.ts", "import '$stores/auth';\n")
    _write(tmp_path, "lib/none.ts", "export const x = 1;\n")

    found = sorted(p.relative_to(tmp_path).as_posix() for p in store_importer_candidates(tmp_path))

    assert found == ["src/components/A.svelte", "src/lib/y.ts"]
