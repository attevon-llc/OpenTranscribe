"""Adapter registry — one entry per injectable eval corpus.

Adding a corpus means writing a :class:`~.base.CorpusAdapter` subclass and one
line here. The injection core, the manifest and the CLI are corpus-agnostic and
do not change.

``options`` carries per-corpus knobs from the CLI (the synthetic tier's meeting
budget, say). It is a dict rather than extra parameters so a new knob does not
change every builder's signature.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.scripts.corpus_injection.adapters.base import CorpusAdapter
from app.scripts.corpus_injection.adapters.qmsum import QMSumAdapter
from app.scripts.corpus_injection.adapters.synthetic import DEFAULT_MEETING_BUDGET
from app.scripts.corpus_injection.adapters.synthetic import DEFAULT_SELECT_FOR
from app.scripts.corpus_injection.adapters.synthetic import SyntheticAdapter

Builder = Callable[[Path, Path, dict[str, Any]], CorpusAdapter]

# Default sub-directory of $RAG_EVAL_DATA_DIR each corpus lives in, and how to
# build its adapter. Timed-reference roots are wired here rather than inside the
# adapter so the CLI can point at a relocated copy.
_BUILDERS: dict[str, tuple[str, Builder]] = {
    "qmsum": (
        "qmsum",
        lambda root, data_dir, _options: QMSumAdapter(
            root, ami_root=data_dir / "ami", icsi_root=data_dir / "icsi"
        ),
    ),
    "synthetic": (
        "synthetic",
        lambda root, _data_dir, options: SyntheticAdapter(
            root,
            meetings=int(options.get("meetings", DEFAULT_MEETING_BUDGET)),
            select_for=tuple(options.get("select_for") or DEFAULT_SELECT_FOR),
        ),
    ),
}

AVAILABLE = tuple(sorted(_BUILDERS))


def build_adapter(
    key: str,
    data_dir: Path,
    root: Path | None = None,
    options: dict[str, Any] | None = None,
) -> CorpusAdapter:
    """Instantiate the adapter for ``key``.

    Args:
        key: Corpus key, one of :data:`AVAILABLE`.
        data_dir: ``$RAG_EVAL_DATA_DIR`` — the parent holding every corpus.
        root: Override for this corpus's own directory.
        options: Per-corpus knobs; unknown keys are ignored by every builder.

    Raises:
        KeyError: Unknown corpus key.
        FileNotFoundError: The resolved root does not exist.
    """
    if key not in _BUILDERS:
        raise KeyError(f"Unknown corpus '{key}'. Available: {', '.join(AVAILABLE)}")
    subdir, builder = _BUILDERS[key]
    resolved = Path(root) if root else Path(data_dir) / subdir
    if not resolved.is_dir():
        raise FileNotFoundError(f"Corpus '{key}' not found at {resolved}")
    return builder(resolved, Path(data_dir), options or {})


__all__ = ["AVAILABLE", "CorpusAdapter", "QMSumAdapter", "SyntheticAdapter", "build_adapter"]
