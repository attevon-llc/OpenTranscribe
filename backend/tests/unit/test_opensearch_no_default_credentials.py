"""OpenSearch must not fall back to a literal admin/admin credential (issue #858).

`docker-compose.yml` disables the OpenSearch security plugin unconditionally for
every deployment mode, so today `admin`/`admin` sent to the cluster is inert. But
it is a silent, wrong-by-design fallback: if the plugin is ever turned on (a
future default-flip, or an operator enabling it by hand) with no credential
configured, `admin`/`admin` against a freshly bootstrapped OpenSearch security
plugin **is the plugin's own documented initial superuser password** -- the
coded default would then silently succeed with a well-known credential instead
of failing loudly. An empty credential fails closed instead: opensearch-py
sends no meaningful `Authorization` header, so a security-enabled cluster
answers 401 rather than granting access.

``OPENSEARCH_USER``/``OPENSEARCH_PASSWORD`` are read once at class-body-execution
time (``os.getenv(...)`` in the class body), so this must run in a clean child
process with the two variables unset -- monkeypatching an already-imported
``settings`` singleton cannot observe the coded default (see
``tests/unit/conftest.py``'s module docstring for why).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PY = REPO_ROOT / "backend" / "app" / "core" / "config.py"
CREATE_INDEXES_PY = REPO_ROOT / "backend" / "scripts" / "create_opensearch_indexes.py"


def test_opensearch_user_has_no_admin_default_with_env_unset(run_in_clean_process):
    output = run_in_clean_process(
        "from app.core.config import settings; print(repr(settings.OPENSEARCH_USER))",
        unset=("OPENSEARCH_USER", "OPENSEARCH_PASSWORD"),
    )
    assert output == repr(""), (
        f"OPENSEARCH_USER defaults to {output} with no env var set -- expected an "
        f"empty string, never a literal credential"
    )


def test_opensearch_password_has_no_admin_default_with_env_unset(run_in_clean_process):
    output = run_in_clean_process(
        "from app.core.config import settings; print(repr(settings.OPENSEARCH_PASSWORD))",
        unset=("OPENSEARCH_USER", "OPENSEARCH_PASSWORD"),
    )
    assert output == repr(""), (
        f"OPENSEARCH_PASSWORD defaults to {output} with no env var set -- expected an "
        f"empty string, never a literal credential"
    )


def test_config_py_source_has_no_literal_admin_default():
    """Belt-and-braces static check directly on the source, so a reviewer sees the
    exact line that would regress without needing to run a subprocess."""
    text = CONFIG_PY.read_text(encoding="utf-8")
    assert not re.search(r'OPENSEARCH_USER:.*os\.getenv\("OPENSEARCH_USER",\s*"admin"\)', text), (
        "config.py still hardcodes an 'admin' fallback for OPENSEARCH_USER"
    )
    assert not re.search(
        r'OPENSEARCH_PASSWORD:.*os\.getenv\("OPENSEARCH_PASSWORD",\s*"admin"\)', text
    ), "config.py still hardcodes an 'admin' fallback for OPENSEARCH_PASSWORD"


def test_create_opensearch_indexes_script_has_no_second_admin_default():
    """backend/scripts/create_opensearch_indexes.py built its own client with a
    SECOND hardcoded admin/admin, bypassing the repo's one shared connection-kwargs
    builder (app/core/opensearch_auth.py). It must use that builder, not its own
    constants."""
    text = CREATE_INDEXES_PY.read_text(encoding="utf-8")
    assert "opensearch_connection_kwargs" in text, (
        "create_opensearch_indexes.py does not use the shared "
        "opensearch_connection_kwargs() builder -- app/core/CLAUDE.md requires every "
        "OpenSearch(...) client go through it"
    )
    assert 'os.getenv("OPENSEARCH_USER", "admin")' not in text
    assert 'os.getenv("OPENSEARCH_PASSWORD", "admin")' not in text
