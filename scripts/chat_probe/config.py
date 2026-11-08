"""Repo paths, the ``backend`` import path, logger and dev-stack defaults for the chat-RAG probe."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

logger = logging.getLogger('probe_chat_rag')

#: Test credentials documented throughout this repo (backend/tests/CLAUDE.md,
#: e2e/conftest.py) — not a secret, just the seeded dev-stack admin account.
DEFAULT_EMAIL = 'admin@example.com'
DEFAULT_PASSWORD = 'password'  # documented dev-stack test credential, not a secret
