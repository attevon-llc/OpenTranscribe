"""Teardown for a file a live-stack integration test uploaded through the real API.

``backend/tests/CLAUDE.md``'s hygiene rule for uploads is "API delete, falling back to
``/force``". A single unchecked ``DELETE`` does not meet it: a file whose follow-on stages
(search indexing, analytics, summarization, ...) are still running answers **409**, which is
not a ``requests`` exception, so a bare call silently leaves the upload in the library.

The e2e suite's ``delete_media_file`` (``tests/e2e/conftest.py``) does the same job, but it
lives in a conftest and cannot be imported from here.
"""

from __future__ import annotations

import time
import warnings

import requests

#: How long to keep retrying the plain delete past a 409 before escalating to ``/force``.
DELETE_RETRY_BUDGET_S = 90.0
_RETRY_PAUSE_S = 5.0
_GONE = (200, 204, 404)


def delete_uploaded_file(base_url: str, headers: dict[str, str], file_uuid: str) -> bool:
    """Delete ``file_uuid``, retrying the plain delete, then falling back to ``/force``.

    Never raises: this runs in a ``finally``, where a cleanup error would mask the test's
    own failure. A file that could not be removed is reported with a warning naming it, so
    the leak is visible in the run's output rather than discovered in the library later.

    Args:
        base_url: API root, e.g. ``http://localhost:5174/api``.
        headers: Auth headers for an account allowed to delete (``/force`` is admin-only).
        file_uuid: The uploaded file.

    Returns:
        Whether the file is confirmed gone.
    """
    deadline = time.monotonic() + DELETE_RETRY_BUDGET_S
    last = "no attempt completed"
    while True:
        try:
            resp = requests.delete(f"{base_url}/files/{file_uuid}", headers=headers, timeout=30)
            if resp.status_code in _GONE:
                return True
            last = f"DELETE -> {resp.status_code} {resp.text[:200]}"
        except requests.RequestException as exc:
            last = f"DELETE raised {exc!r}"
        if time.monotonic() + _RETRY_PAUSE_S >= deadline:
            break
        time.sleep(_RETRY_PAUSE_S)

    try:
        resp = requests.delete(f"{base_url}/files/{file_uuid}/force", headers=headers, timeout=30)
        if resp.status_code in _GONE:
            return True
        last = f"{last}; force DELETE -> {resp.status_code} {resp.text[:200]}"
    except requests.RequestException as exc:
        last = f"{last}; force DELETE raised {exc!r}"
    warnings.warn(
        f"could not delete test upload {file_uuid} -- it is LEFT in the library: {last}",
        stacklevel=2,
    )
    return False
