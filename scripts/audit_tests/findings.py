"""Finding record and the detector category list."""

from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = (
    'permissive-status',
    'negated-status',
    'status-guarded-assert',
    'conditional-only',
    'conditional-skip',
    'loop-only',
    'no-assertion',
    'unfalsifiable',
    'weak-only',
    'mock-only',
    'failure-masking',
    'error-swallowed',
    'mock-heavy',
    'fixture-named-test',
    'external-service-mock',
    'readiness-probe-target',
    'broad-raises',
    'bare-return',
    'self-comparison',
    'sleep-sync',
    'skipped-test',
)


@dataclass(frozen=True)
class Finding:
    """One suspect test."""

    category: str
    path: str
    line: int
    test: str
    detail: str

    @property
    def key(self) -> str:
        """Allowlist key. Includes the category so one exemption cannot cover all six."""
        return f'{self.path}::{self.test}::{self.category}'
