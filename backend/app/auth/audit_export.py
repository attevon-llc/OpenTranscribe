"""The one audit-log export writer, shared by the super-admin and organization-admin routes.

``GET /admin/audit-logs/export`` (all tenants) and ``GET /org-admin/audit-logs/export`` (one
organization) both read through ``query_audit_logs`` and render here, so the column set, the
spreadsheet-formula neutralisation and the file naming cannot drift between them (issue #1277).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import Response

from app.utils.content_disposition import attachment_header
from app.utils.csv_safe import render_csv

#: The stored fields an export carries, in column order. ``details`` is serialised to one cell.
EXPORT_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "event_type",
    "outcome",
    "user_id",
    "username",
    "target_user_id",
    "organization_id",
    "tenant_scope",
    "resource_type",
    "resource_uuid",
    "source_ip",
    "user_agent",
    "error_code",
    "details",
)

#: Read-time display columns the organization export appends (see ``services/org_audit_view``).
DISPLAY_COLUMNS: tuple[str, ...] = ("actor_display", "target_display", "resource_display")


def _cell(log: dict[str, Any], column: str) -> Any:
    value = log.get(column)
    if isinstance(value, dict | list):
        return json.dumps(value, default=str)
    return value


def render_audit_csv(
    logs: Sequence[dict[str, Any]], columns: Sequence[str] = EXPORT_COLUMNS
) -> str:
    """CSV text with a header row (always) and every cell formula-neutralised."""
    return render_csv(columns, ([_cell(log, c) for c in columns] for log in logs))


def audit_export_response(
    logs: Sequence[dict[str, Any]],
    export_format: str,
    *,
    filename_prefix: str,
    columns: Sequence[str] = EXPORT_COLUMNS,
    truncated: bool = False,
) -> Response:
    """The download for ``logs`` as ``csv`` or ``json``.

    The filename is built from a fixed prefix and the date alone: nothing from a record
    reaches a header.
    """
    filename = f"{filename_prefix}-{datetime.now(UTC):%Y%m%d}.{export_format}"
    if export_format == "json":
        body = json.dumps(list(logs), indent=2, default=str)
        media_type = "application/json"
    else:
        body = render_audit_csv(logs, columns)
        media_type = "text/csv"
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": attachment_header(filename),
            "X-Export-Truncated": "true" if truncated else "false",
        },
    )
