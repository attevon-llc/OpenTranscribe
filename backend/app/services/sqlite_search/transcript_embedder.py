"""Local ONNX MiniLM transcript embeddings for SQLite search."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np

DEFAULT_MODEL_DIR = Path.home() / ".cache/openwhispr/embedding-models/all-MiniLM-L6-v2"


def _normalize(matrix: np.ndarray) -> np.ndarray:
    """Return row-wise L2-normalized float32 vectors."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return (matrix / np.maximum(norms, 1e-12)).astype(np.float32)


def _mean_pool(hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Mean-pool token embeddings with attention mask."""
    expanded = mask[..., None].astype(np.float32)
    summed = (hidden * expanded).sum(axis=1)
    counts = np.maximum(expanded.sum(axis=1), 1e-12)
    return summed / counts


class LocalMiniLMEmbedder:
    """No-egress ONNX all-MiniLM-L6-v2 embedder."""

    def __init__(self, model_dir: str | Path = DEFAULT_MODEL_DIR):
        self.model_dir = Path(model_dir).expanduser()
        self._session = None
        self._tokenizer = None

    def _load(self) -> None:
        """Load tokenizer and ONNX session lazily from local files."""
        if self._session is not None and self._tokenizer is not None:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tokenizer_path = self.model_dir / "tokenizer.json"
        model_path = self.model_dir / "model.onnx"
        if not tokenizer_path.exists() or not model_path.exists():
            raise FileNotFoundError(f"local MiniLM files missing in {self.model_dir}")
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])

    def encode(self, texts: Iterable[str], max_length: int = 256) -> list[list[float]]:
        """Encode texts as normalized 384-d vectors."""
        self._load()
        items = [str(text or "") for text in texts]
        if not items:
            return []
        enc = self._tokenizer.encode_batch(items)  # type: ignore[union-attr]
        ids = [item.ids[:max_length] for item in enc]
        masks = [item.attention_mask[:max_length] for item in enc]
        width = max(len(row) for row in ids)
        input_ids = np.array([row + [0] * (width - len(row)) for row in ids], dtype=np.int64)
        attention = np.array([row + [0] * (width - len(row)) for row in masks], dtype=np.int64)
        token_types = np.zeros_like(input_ids, dtype=np.int64)
        hidden = self._session.run(  # type: ignore[union-attr]
            None,
            {"input_ids": input_ids, "attention_mask": attention, "token_type_ids": token_types},
        )[0]
        pooled = _mean_pool(hidden, attention)
        return _normalize(pooled).tolist()
