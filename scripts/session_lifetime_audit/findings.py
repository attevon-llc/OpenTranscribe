"""The detector categories and the ``Finding`` record."""

from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = (
    'session-subprocess',
    'session-object-storage',
    'session-search-engine',
    'session-http',
    'session-llm',
    'session-model',
    'session-smtp',
    'session-thread-pool',
    'session-param-slow-work',
)


@dataclass(frozen=True)
class Finding:
    """One slow call reachable with a DB transaction open."""

    category: str
    path: str
    line: int
    scope: str
    detail: str

    @property
    def key(self) -> str:
        """Allowlist key. Includes the category so one exemption cannot cover all nine."""
        return f'{self.path}::{self.scope}::{self.category}'
