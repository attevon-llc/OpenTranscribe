"""Every test that seeds the scale dataset runs in the one ``scale_dataset_seed`` xdist group.

The seeders write fixed natural keys (``perf-N@loadtest.invalid``) into rows the test never
commits. Two such tests on different workers make the second INSERT wait on the first's
unique-index entry (``transactionid`` ShareLock) while it still holds the shared DDL advisory
lock, so a queued ``ddl_exclusive`` test stalls and every later test queues behind it.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
SEEDERS = re.compile(r"\b(seed_all|seed_activity_only|seed_dataset)\b")
GROUP = 'pytest.mark.xdist_group("scale_dataset_seed")'


def _seeding_test_files() -> list[Path]:
    return sorted(
        p
        for p in TESTS.rglob("test_*.py")
        if p.name != Path(__file__).name and SEEDERS.search(p.read_text())
    )


def test_every_file_that_seeds_the_scale_dataset_is_in_the_seed_group() -> None:
    files = _seeding_test_files()
    assert len(files) >= 5, "the detector found fewer seeding test files than exist"
    ungrouped = [p.name for p in files if GROUP not in p.read_text()]
    assert ungrouped == [], f"seeding tests outside the scale_dataset_seed group: {ungrouped}"
