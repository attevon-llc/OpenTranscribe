"""Synthetic meeting corpus generator with ground truth known by construction.

See ``.rag-403/synthetic-tier-design.md`` for the method and
``cli.py`` for the command-line interface.
"""

from .corpus import GENERATOR_VERSION
from .corpus import build_corpus
from .corpus import default_config
from .measure import measure_corpus
from .validate import validate_corpus

__all__ = [
    "GENERATOR_VERSION",
    "build_corpus",
    "default_config",
    "measure_corpus",
    "validate_corpus",
]
