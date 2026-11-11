"""Guard: importing every pydantic-bearing module must emit no PydanticDeprecatedSince20.

Class-based ``class Config:`` (and the other v1 idioms) only warn at class-creation time,
i.e. at first import, and pytest's own import caching hides that from later tests. The
check therefore runs in a fresh interpreter with the warning promoted to an error.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

_SCRIPT = """
import importlib, pkgutil, sys, warnings
from pydantic.warnings import PydanticDeprecatedSince20

warnings.simplefilter("error", PydanticDeprecatedSince20)
count = 0
for pkg_name in ("app.schemas", "app.models"):
    pkg = importlib.import_module(pkg_name)
    for info in pkgutil.walk_packages(pkg.__path__, pkg_name + "."):
        importlib.import_module(info.name)
        count += 1
print("IMPORTED", count)
"""


def test_schema_and_model_modules_import_without_pydantic_v1_deprecations(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "TEMP_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    imported = int(result.stdout.rsplit("IMPORTED", 1)[1])
    assert imported > 50, result.stdout
