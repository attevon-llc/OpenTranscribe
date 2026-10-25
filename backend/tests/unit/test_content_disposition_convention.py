"""No route may build a ``Content-Disposition`` value from text by hand (issue #1283).

An f-string around a user-controlled name is two bugs in one: a non-ASCII name raises
``UnicodeEncodeError`` when Starlette encodes the header latin-1 (an HTTP 500 on export), and a
quote or CR/LF in the name breaks out of the header. Both fixed once in
``app.utils.content_disposition.attachment_header``; this walks every module under ``app/`` and
fails on any header value that is not either a constant string or a call to that helper, so the
next export route cannot reintroduce the shape.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"
_HELPER = "attachment_header"


def _is_disposition_key(node: ast.expr | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "content-disposition" in node.value.lower()
    )


def _value_is_safe(value: ast.expr) -> bool:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return True
    if isinstance(value, ast.Call):
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        return name == _HELPER
    return False


def disposition_offences(source: str) -> list[int]:
    """Line numbers of Content-Disposition values that are neither constant nor the helper."""
    offences: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if _is_disposition_key(key) and not _value_is_safe(value):
                    offences.append(value.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and _is_disposition_key(target.slice)
                    and not _value_is_safe(node.value)
                ):
                    offences.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"append", "setdefault", "__setitem__", "set"}
            and len(node.args) >= 2
            and _is_disposition_key(node.args[0])
            and not _value_is_safe(node.args[1])
        ):
            offences.append(node.lineno)
    return offences


def _app_modules() -> list[Path]:
    return sorted(_APP_ROOT.rglob("*.py"))


def test_no_module_builds_a_content_disposition_value_by_hand() -> None:
    found = {
        path.relative_to(_APP_ROOT).as_posix(): lines
        for path in _app_modules()
        if (lines := disposition_offences(path.read_text(encoding="utf-8")))
    }
    assert found == {}, (
        "Content-Disposition built by hand; use app.utils.content_disposition."
        f"attachment_header(name): {found}"
    )


def test_the_scan_actually_covers_the_download_routes() -> None:
    """A scanner that walks nothing reports zero offences, same as a clean tree."""
    modules = _app_modules()
    assert len(modules) > 200
    users = [
        p.relative_to(_APP_ROOT).as_posix()
        for p in modules
        if _HELPER in p.read_text(encoding="utf-8") and p.name != "content_disposition.py"
    ]
    assert "api/endpoints/files/transcript_export.py" in users
    assert "api/endpoints/files/subtitles.py" in users
    assert "api/endpoints/summarization.py" in users


@pytest.mark.parametrize(
    "snippet",
    [
        'h = {"Content-Disposition": f\'attachment; filename="{name}"\'}',
        'h = {"Content-Disposition": "attachment; filename=" + name}',
        'h = {"Content-Disposition": "attachment; filename={}".format(name)}',
        'h = {"Content-Disposition": "attachment; filename=%s" % name}',
        'h = {"Content-Disposition": value}',
        'h = {"content-disposition": f"inline; filename={name}"}',
        'headers["Content-Disposition"] = f\'attachment; filename="{name}"\'',
        'r.headers.append("Content-Disposition", f"attachment; filename={name}")',
        'extra = {"response-content-disposition": f"attachment; filename={name}"}',
    ],
)
def test_the_scan_fires_on_a_hand_built_value(snippet: str) -> None:
    assert disposition_offences(textwrap.dedent(snippet)) == [1]


@pytest.mark.parametrize(
    "snippet",
    [
        'h = {"Content-Disposition": "attachment; filename=custom_vocabulary.json"}',
        'h = {"Content-Disposition": attachment_header(name)}',
        'h = {"Content-Disposition": attachment_header(f"{base}.{ext}", disposition="inline")}',
        'h = {"Content-Disposition": content_disposition.attachment_header(name)}',
        'h = {"X-Other": f"attachment; filename={name}"}',
    ],
)
def test_the_scan_stays_quiet_on_a_safe_value(snippet: str) -> None:
    assert disposition_offences(textwrap.dedent(snippet)) == []
