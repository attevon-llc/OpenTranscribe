"""The invariant the whole document tier split rests on: **the slim tier never imports torch.**

Tier 1 (``backends/docling_slim.py``) runs *in-process* inside the existing Celery workers —
the CPU worker and ``celery-redaction`` among them. Those workers are deliberately not GPU
workers and must stay light. If any import on the slim path reaches ``torch``, the tier
stops being in-process: it drags a multi-gigabyte CUDA stack into every worker that touches
a document, and the reason for having a sidecar at all disappears.

This is not a hypothetical. Phase 0 measured that
``docling.document_converter.DocumentConverter`` **cannot be imported without torch** — its
module body reaches ``docling_ibm_models`` → ``torch``. The slim tier therefore instantiates
Docling's declarative backends directly and never touches ``DocumentConverter``. That is a
*discipline*, enforced by nothing, one convenience import away from being undone. This file
is the enforcement.

**Why a subprocess.** An in-process check is worthless: pytest runs thousands of tests in one
interpreter and any one of them may have imported torch already, at which point
``sys.modules`` has it and every later import is a cache hit that no hook can see. The guard
has to run in an interpreter that has never imported torch, with a ``sys.meta_path`` finder
that turns the import into an error. Nothing less can fail.

**Why it parses rather than only importing.** Docling's backends are resolved lazily inside
:meth:`DoclingSlimParser.parse`, so importing the module proves almost nothing — the torch
reach would happen on the first document. The child parses one document per declarative
format, in-memory, so the lazy imports actually run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Run in a fresh interpreter with torch made unimportable, then exercise the slim path.
#: Printed verdict is a single JSON line on the last line of stdout.
_GUARD_SCRIPT = r'''
import json, sys, traceback

BANNED = {"torch", "torchvision", "torchaudio"}

class _BanTorch:
    """A meta-path finder that turns `import torch` into an error, anywhere in the tree."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BANNED:
            raise ImportError(f"BANNED-IMPORT:{fullname}")
        return None

sys.meta_path.insert(0, _BanTorch())

verdict = {"import_ok": False, "parsed": {}, "failed": {}, "torch_in_modules": False,
           "docling_available": None, "error": None}
try:
    from app.services.documents import chunking, detect, ir, progress, registry, safety  # noqa: F401
    from app.services.documents.backends.docling_slim import DoclingSlimParser
    from app.services.documents.types import ParseOptions, ParseSource
    verdict["import_ok"] = True

    parser = DoclingSlimParser()
    available, detail = parser.health()
    verdict["docling_available"] = bool(available)
    verdict["health_detail"] = str(detail)

    # In-memory documents, so this runs with no corpus and in CI. One per declarative
    # backend plus the two native paths, because the lazy import is per-format.
    samples = {
        "text/plain": b"A plain text document.\n\nSecond paragraph.\n",
        "text/markdown": b"# Heading\n\nBody text with a **bold** word.\n\n- item one\n- item two\n",
        "text/html": b"<html><body><h1>Title</h1><p>Body text.</p></body></html>",
        "text/csv": b"name,cost\nwidget,42\ngadget,7\n",
        "text/tab-separated-values": b"name\tcost\nwidget\t42\n",
    }
    for mime, data in samples.items():
        name = "sample." + mime.rsplit("/", 1)[1].split("-")[0]
        try:
            document = parser.parse(
                ParseSource(filename=name, mime=mime, data=data), options=ParseOptions()
            )
            ir.validate_ir(document)
            verdict["parsed"][mime] = len(document.text)
        except Exception as exc:
            verdict["failed"][mime] = f"{type(exc).__name__}: {exc}"[:300]
except Exception:
    verdict["error"] = traceback.format_exc()[-2000:]

verdict["torch_in_modules"] = any(m.split(".")[0] in BANNED for m in sys.modules)
print(json.dumps(verdict))
'''


def _run_guard() -> dict:
    env = dict(os.environ)
    env.setdefault("DATA_DIR", str(BACKEND_ROOT / "data"))
    env.setdefault("MODELS_DIR", str(BACKEND_ROOT / "models"))
    env.setdefault("TEMP_DIR", str(BACKEND_ROOT / "temp"))
    env["PYTHONPATH"] = str(BACKEND_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _GUARD_SCRIPT],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(BACKEND_ROOT),
        env=env,
    )
    assert completed.stdout.strip(), (
        f"the guard subprocess printed no verdict (exit {completed.returncode}).\n"
        f"stderr:\n{completed.stderr[-2000:]}"
    )
    # Annotated rather than returned directly: json.loads is typed as Any, and
    # returning it straight out of a dict-declared function trips no-any-return.
    verdict: dict = json.loads(completed.stdout.strip().splitlines()[-1])
    return verdict


@pytest.fixture(scope="module")
def guard() -> dict:
    return _run_guard()


def test_the_slim_tier_imports_with_torch_made_unimportable(guard):
    """The import half. A convenience ``from docling.document_converter import ...`` at the
    top of the slim backend fails here and nowhere else in the suite."""
    assert guard["error"] is None, f"importing the slim path reached torch:\n{guard['error']}"
    assert guard["import_ok"]


def test_parsing_every_in_process_format_never_reaches_torch(guard):
    """The parse half — the one that matters, because the backends are imported lazily.

    Docling resolves each declarative backend on first use, so an import added inside
    ``_convert_declarative`` would sail past an import-only check and only fail in
    production, on the first document a CPU worker touched.
    """
    if not guard["docling_available"]:
        pytest.skip(f"docling is not installed in this environment: {guard.get('health_detail')}")

    assert not guard["failed"], (
        f"{len(guard['failed'])} format(s) could not be parsed with torch banned: {guard['failed']}"
    )
    # Every sample really produced text; a backend that returned an empty document would
    # otherwise satisfy "did not raise".
    assert set(guard["parsed"]) == {
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
        "text/tab-separated-values",
    }
    for mime, length in guard["parsed"].items():
        assert length > 10, f"{mime} parsed to {length} characters — that is not a parse"


def test_torch_is_genuinely_absent_from_the_child_interpreter(guard):
    """Guards the guard.

    If the ban hook silently stopped working — a ``sys.meta_path`` reordering, an import
    that bypasses the finder — the two tests above would pass by doing nothing, which is
    indistinguishable from a clean result. This asserts the child really finished with no
    torch module loaded, so the ban was in force for the whole run.
    """
    assert guard["torch_in_modules"] is False, (
        "torch is present in the child's sys.modules, so the ban hook was not in force and "
        "the assertions above proved nothing"
    )
