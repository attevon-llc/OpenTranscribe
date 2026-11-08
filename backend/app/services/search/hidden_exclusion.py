"""The caller's hidden files, as an OpenSearch filter clause (per-user hide, issue #1102).

Hide is applied READ-side and per request: the index documents are shared by every user who can
read a file, so they are never stamped with anyone's hide. Each search, count, suggestion and
facet query of the caller instead carries one ``must_not terms file_uuid`` clause built from
``hidden_media_service.hidden_uuids``, which the per-user cap keeps far below
``index.max_terms_count``. A request explicitly scoped to files the caller picked
(``file_uuid``) is not given the clause: hide is a view preference, not access control.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def exclude_files_clauses(file_uuids: Sequence[str] | None) -> list[dict[str, Any]]:
    """Zero or one filter clause dropping every document of ``file_uuids``."""
    if not file_uuids:
        return []
    return [{"bool": {"must_not": {"terms": {"file_uuid": list(file_uuids)}}}}]
