"""Adapter registry — one entry per injectable eval corpus.

Adding a corpus means writing a :class:`~.base.CorpusAdapter` subclass and one
line here. The injection core, the manifest and the CLI are corpus-agnostic and
do not change.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.scripts.corpus_injection.adapters.base import CorpusAdapter
from app.scripts.corpus_injection.adapters.generic_json import GenericJsonAdapter
from app.scripts.corpus_injection.adapters.qmsum import QMSumAdapter

# Default sub-directory of $RAG_EVAL_DATA_DIR each corpus lives in, and how to
# build its adapter. Timed-reference roots are wired here rather than inside the
# adapter so the CLI can point at a relocated copy.
_BUILDERS: dict[str, tuple[str, Callable[[Path, Path], CorpusAdapter]]] = {
    "qmsum": (
        "qmsum",
        lambda root, data_dir: QMSumAdapter(
            root, ami_root=data_dir / "ami", icsi_root=data_dir / "icsi"
        ),
    ),
    "synthetic": (
        "synthetic",
        lambda root, _data_dir: GenericJsonAdapter(root, key="synthetic", name="Synthetic corpus"),
    ),
}

AVAILABLE = tuple(sorted(_BUILDERS))


def build_adapter(key: str, data_dir: Path, root: Path | None = None) -> CorpusAdapter:
    """Instantiate the adapter for ``key``.

    Args:
        key: Corpus key, one of :data:`AVAILABLE`.
        data_dir: ``$RAG_EVAL_DATA_DIR`` — the parent holding every corpus.
        root: Override for this corpus's own directory.

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
    return builder(resolved, Path(data_dir))


__all__ = ["AVAILABLE", "CorpusAdapter", "GenericJsonAdapter", "QMSumAdapter", "build_adapter"]
