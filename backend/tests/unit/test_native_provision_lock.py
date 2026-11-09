"""Concurrent API workers export the native diarizer's models once, not once each."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]

_CALLER = textwrap.dedent(
    """
    from app.transcription import native_provision as np
    np._subprocess_env = lambda: __import__("os").environ.copy()
    print(np.ensure_native_models().reason)
    """
)

#: Stands in for ``diar-server``: exports (slowly) unless the marker is already there.
_FAKE_EXPORTER = textwrap.dedent(
    """\
    #!/bin/sh
    dir=""
    while [ $# -gt 0 ]; do
      if [ "$1" = "--models-dir" ]; then dir="$2"; fi
      shift
    done
    if [ ! -f "$dir/diar-provision.json" ]; then
      echo run >> "$dir/exporter-runs.log"
      sleep 2
      echo '{}' > "$dir/diar-provision.json"
    fi
    echo '{}'
    """
)


def test_two_workers_starting_together_run_the_exporter_once(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exporter = bin_dir / "diar-server"
    exporter.write_text(_FAKE_EXPORTER)
    exporter.chmod(0o755)
    models = tmp_path / "models"
    models.mkdir()
    clip = tmp_path / "smoke.wav"
    clip.write_bytes(b"RIFF")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DIAR_MODELS_DIR": str(models),
        "DIAR_NATIVE_SMOKE_CLIP": str(clip),
        "PYTHONPATH": str(_BACKEND),
    }

    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _CALLER],
            env=env,
            cwd=_BACKEND,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [worker.communicate(timeout=120) for worker in workers]

    assert [w.returncode for w in workers] == [0, 0], outputs
    assert (models / "exporter-runs.log").read_text().split() == ["run"]
    assert sorted(out.strip() for out, _ in outputs) == ["already provisioned", "exported"]
