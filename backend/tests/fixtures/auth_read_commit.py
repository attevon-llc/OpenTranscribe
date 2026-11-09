"""Recognise the commit the auth dependencies make to end their read transaction.

``end_read_transaction`` (``app/db/session_utils.py``) commits the request session so the
connection goes back to the pool before the handler runs. A test that makes "the handler's
commit" fail or counts it must let that one through, or it fires on authentication instead.
"""

from __future__ import annotations

import sys


def is_auth_read_commit() -> bool:
    """True when the patched ``commit`` calling this was invoked by ``end_read_transaction``."""
    return sys._getframe(2).f_code.co_name == "end_read_transaction"
