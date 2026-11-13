"""The concurrency sweep may only recreate a --fresh stack and never writes .env.

It used to ``sed -i`` the live ``.env`` and ``./opentr.sh stop``/``start`` the live stack for each
level (issue #1195 patched the restore, not the cause). Compose lets an exported shell variable beat
``.env`` in interpolation (docs.docker.com/compose/how-tos/environment-variables/envvars-precedence),
so the level is exported for ``./opentr.sh start dev --fresh NAME`` instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_concurrency_sweep.sh"


def test_refuses_without_fresh_and_touches_nothing(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--max-conc", "1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "--fresh NAME is required" in result.stderr
    assert list(tmp_path.iterdir()) == [], "a refused run must not create output or edit files"


def test_source_never_writes_dotenv_or_stops_the_live_stack() -> None:
    code = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert "sed -i" not in code
    assert "> .env" not in code and ">> .env" not in code
    assert "opentr.sh stop" not in code
    assert 'start dev --fresh "$FRESH"' in code
    assert 'export GPU_CONCURRENT_REQUESTS="$conc"' in code
