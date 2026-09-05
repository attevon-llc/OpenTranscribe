"""`backend/.dockerignore` must exclude EVERY developer venv, by glob rather than by name.

Found while measuring the lite image for #660. The file listed `venv/`, `venv.bak/` and
`.venv/` — but not `venv-eval/`, which is not a stray directory someone happened to make: it is
one this repo's own documentation instructs you to create
(``docs-site/docs/developer-guide/rag-evaluation.md``: *"python3.12 -m venv backend/venv-eval"*),
for the RAGAS judge tier that must stay out of the app's dependency tree.

It is gitignored, so no git-facing check could see it, and `Dockerfile.lite`'s `COPY . .` put
all 5.6 GB of it into `/app`. Measured on this host: the lite image went **2.6 GB -> 8.4 GB**,
and the blow-up presented as a suspected dependency regression from a one-line requirements
change. Any image built on a developer machine that followed the documented eval workflow would
ship it.

The fix is a glob, so this test asserts the glob rather than asserting that one more name was
added to a list — a list is what failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERIGNORE = REPO_ROOT / "backend" / ".dockerignore"

pytestmark = pytest.mark.skipif(
    not DOCKERIGNORE.exists(), reason="backend/.dockerignore not in this checkout"
)


def _patterns() -> list[str]:
    return [
        ln.strip()
        for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _is_ignored(name: str) -> bool:
    """Does any pattern exclude a top-level directory called `name`?

    Docker matches with Go's filepath.Match semantics; `fnmatch` agrees for the simple
    directory globs this file uses. A trailing slash means "directory", so it is stripped
    before matching.
    """
    from fnmatch import fnmatch

    return any(fnmatch(name, p.rstrip("/")) for p in _patterns())


@pytest.mark.parametrize(
    "venv_dir",
    [
        "venv",  # the host venv every contributor has (backend/CLAUDE.md)
        "venv-eval",  # the RAG judge venv the docs tell you to create — the one that was missed
        "venv.bak",
        ".venv",
        "venv-scratch",  # any future sibling: the point of the glob is that it needs no edit
    ],
)
def test_every_developer_venv_is_excluded_from_the_build_context(venv_dir: str):
    assert _is_ignored(venv_dir), (
        f"backend/.dockerignore does not exclude {venv_dir!r}, so it lands in the image via "
        f"Dockerfile's `COPY . .`. backend/venv-eval alone is 5.6 GB and tripled the lite "
        f"image. Use a glob (venv*/), not another name in a list — a list is what failed."
    )


def test_the_exclusion_is_a_glob_not_an_enumeration():
    """Guards the FIX, not just its effect.

    Without this, someone could satisfy every case above by appending `venv-eval/` and
    `venv-scratch/` literally, and the next documented venv would reintroduce the bug.
    """
    patterns = _patterns()
    assert any(
        p.rstrip("/") == "venv*" for p in patterns
    ), f"expected a `venv*/` glob in backend/.dockerignore, found only {
        [p for p in patterns if 'venv' in p]
    }. Enumerating names is what let venv-eval through."


def test_the_application_itself_is_never_excluded():
    """Control: proves the glob is scoped, not a wildcard that would empty the context.

    `app/` and `alembic/` are the application itself — if a broadened pattern ever excluded
    them the image would build and then fail at import time, which is a far worse failure than
    a large image.
    """
    for needed in ("app", "alembic", "requirements.txt", "requirements-lite.txt"):
        assert not _is_ignored(needed), (
            f"backend/.dockerignore excludes {needed!r} — the image would be missing the "
            f"application itself"
        )
