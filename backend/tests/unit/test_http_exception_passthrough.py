"""A broad ``except Exception`` in an endpoint must not swallow a deliberate HTTPException.

Endpoints raise ``fastapi.HTTPException`` directly (see ``backend/app/api/CLAUDE.md``), and
many wrap their body in ``try/except Exception`` to return an opaque 500 rather than leak a
stack trace. Those two facts collide: ``HTTPException`` **is** an ``Exception``, so without an
explicit passthrough the broad handler converts every deliberate status into a 500.

That is not cosmetic. It was live on ``GET /api/speakers?file_uuid=``, where the 403 from
``get_file_by_uuid_with_permission`` reached the client as
``500 Internal server error while loading speakers`` — an authorization denial reported as a
server fault, logged at error level, and indistinguishable from a real outage. The test that
should have caught it asserted ``status_code in (403, 500)`` and accepted both (issue #431).

It also breaks a documented contract: ``require_capability()`` returns **404** so a gated
router looks like an unknown route. Masked as a 500, a gated route instead looks broken.

56 sites across 22 files had this shape. This module keeps it at zero.
"""

from __future__ import annotations

import ast
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[2] / "app" / "api"

#: Handlers this broad swallow everything, including HTTPException.
_BROAD = ("Exception", "BaseException")

#: Markers that a handler produces a 500.
_FIVE_HUNDRED = ("500", "INTERNAL_SERVER_ERROR", "internal_error")


def _raises_500(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise) and node.exc is not None:
            rendered = ast.unparse(node.exc)
            if any(marker in rendered for marker in _FIVE_HUNDRED):
                return True
    return False


def _caught_name(handler: ast.ExceptHandler) -> str:
    return "bare" if handler.type is None else ast.unparse(handler.type)


def _offenders() -> list[str]:
    """Every ``try`` whose 500-producing broad handler has no HTTPException passthrough."""
    found: list[str] = []
    for path in sorted(_API_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error fails collection anyway
            continue
        rel = path.relative_to(_API_ROOT.parent.parent).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            names = [_caught_name(h) for h in node.handlers]
            for index, handler in enumerate(node.handlers):
                caught = names[index]
                if caught not in _BROAD and caught != "bare":
                    continue
                if not _raises_500(handler):
                    continue
                # An earlier handler naming HTTPException already lets it through.
                if any("HTTPException" in earlier for earlier in names[:index]):
                    continue
                found.append(f"{rel}:{handler.lineno} (catches {caught} -> 500)")
    return found


def test_no_endpoint_masks_a_deliberate_http_status_as_500() -> None:
    offenders = _offenders()
    assert not offenders, (
        "These handlers turn a deliberate HTTPException into a 500, so an intended "
        "401/403/404/422 raised in the same block reaches the client as an internal server "
        "error. Add `except HTTPException:\\n    raise` immediately before the broad "
        "handler:\n  " + "\n  ".join(offenders)
    )


def test_the_detector_recognises_the_bug_it_guards_against() -> None:
    """A guard that cannot fail is worth nothing — prove it fires on the broken shape.

    Written because this whole module exists to catch tests that pass unconditionally.
    """
    broken = ast.parse(
        "try:\n"
        "    do_work()\n"
        "except Exception as e:\n"
        "    raise HTTPException(status_code=500, detail='boom') from e\n"
    )
    fixed = ast.parse(
        "try:\n"
        "    do_work()\n"
        "except HTTPException:\n"
        "    raise\n"
        "except Exception as e:\n"
        "    raise HTTPException(status_code=500, detail='boom') from e\n"
    )

    def is_offending(tree: ast.Module) -> bool:
        node = tree.body[0]
        assert isinstance(node, ast.Try)
        names = [_caught_name(h) for h in node.handlers]
        return any(
            (names[i] in _BROAD or names[i] == "bare")
            and _raises_500(h)
            and not any("HTTPException" in e for e in names[:i])
            for i, h in enumerate(node.handlers)
        )

    assert is_offending(broken), "detector missed the broken shape it exists to find"
    assert not is_offending(fixed), "detector flags the correct shape as broken"
