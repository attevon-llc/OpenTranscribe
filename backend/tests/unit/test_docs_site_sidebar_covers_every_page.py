"""Every docs page must be reachable from ``docs-site/sidebars.ts`` (issue #1310).

A page missing from the sidebar still builds and is still served by URL, so nothing fails: it
is just undiscoverable. The 0.6.1 upgrade notes shipped exactly that way. Docusaurus ignores
files and directories whose name starts with ``_`` (docs "Create a doc" - partials), so those
are exempt by rule; everything else must be listed or sit in ``EXCEPTIONS`` with a reason.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = REPO_ROOT / "docs-site" / "docs"
SIDEBARS = REPO_ROOT / "docs-site" / "sidebars.ts"

EXCEPTIONS: dict[str, str] = {
    "authentication/keycloak": (
        "Deliberate redirect stub ('This page moved to OpenID Connect') kept so old links "
        "resolve; listing it would duplicate the OIDC entry in the nav."
    ),
}


def sidebar_doc_ids(text: str) -> set[str]:
    """Doc ids named in a sidebars.ts source; ``//`` comment lines are ignored."""
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    return set(re.findall(r"""['"]([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)['"]""", live))


def page_ids(docs_dir: Path) -> set[str]:
    ids = set()
    for path in docs_dir.rglob("*"):
        if path.suffix not in {".md", ".mdx"}:
            continue
        rel = path.relative_to(docs_dir).with_suffix("")
        if any(part.startswith("_") for part in rel.parts):
            continue
        ids.add(rel.as_posix())
    return ids


def unlisted(pages: set[str], listed: set[str]) -> list[str]:
    return sorted(p for p in pages if p not in listed and p not in EXCEPTIONS)


def test_every_docs_page_is_in_the_sidebar_or_a_reasoned_exception():
    missing = unlisted(page_ids(DOCS_DIR), sidebar_doc_ids(SIDEBARS.read_text(encoding="utf-8")))
    assert not missing, (
        f"docs pages not listed in docs-site/sidebars.ts (add them, or add a reasoned "
        f"entry to EXCEPTIONS): {missing}"
    )


def test_exceptions_name_real_pages_and_carry_a_reason():
    pages = page_ids(DOCS_DIR)
    stale = [k for k in EXCEPTIONS if k not in pages]
    assert not stale, f"EXCEPTIONS names pages that no longer exist: {stale}"
    assert all(len(reason) > 20 for reason in EXCEPTIONS.values())


def test_scanner_fires_on_an_unlisted_page(tmp_path: Path):
    """Must-fire: a page absent from the sidebar is reported; a commented-out entry does not count."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "listed.md").write_text("x")
    (tmp_path / "a" / "orphan.mdx").write_text("x")
    sidebar = "items: [\n  'a/listed',\n  // 'a/orphan',\n]\n"
    assert unlisted(page_ids(tmp_path), sidebar_doc_ids(sidebar)) == ["a/orphan"]


def test_scanner_stays_clean_and_skips_partials(tmp_path: Path):
    """Must-stay-clean: everything listed passes, and ``_``-prefixed partials are not pages."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "listed.md").write_text("x")
    (tmp_path / "_gen").mkdir()
    (tmp_path / "_gen" / "table.md").write_text("x")
    (tmp_path / "a" / "_part.md").write_text("x")
    assert unlisted(page_ids(tmp_path), sidebar_doc_ids("['a/listed']")) == []
