"""app.core.celery imports its dependencies before it patches torch / configures metrics.

The module patches ``torch.load`` on first torch import and configures prometheus multiprocess
mode before ``prometheus_client`` loads. That is only safe to do *after* the module's own
imports if none of those imports pulls in torch or prometheus_client. This pins it.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

_PROBE = """
import sys
import celery.signals
from app.core import celery_worker_lifecycle, worker_metrics
from app.core.celery_beat import BEAT_SCHEDULE
from app.core.celery_routing import CELERY_QUEUES, TASK_ROUTES
from app.core.config import settings
from app.core.demo_publish import DemoAwareCelery
from app.core.env import env_float, env_int
from app.utils.nltk_offline import ensure_nltk_data_private
print("LOADED", [m for m in ("torch", "prometheus_client") if m in sys.modules])
"""


def test_celery_module_dependencies_do_not_import_torch_or_prometheus():
    import os

    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "TEMP_DIR": tmp}
        result = subprocess.run(
            [sys.executable, "-c", _PROBE],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "LOADED []" in result.stdout, result.stdout
