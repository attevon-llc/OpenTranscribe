"""Tag endpoints, split by concern.

`tags.py` had grown to 740 lines against the repo's ~300 guideline — the
contributor flagged it at 501 and it grew further with sharing, the file list
and the selection query. The split follows the precedent set by
``endpoints/files/`` and ``endpoints/auth/``: one module per concern, one
``APIRouter`` each, assembled here.

* ``crud`` — create, list, unused, cleanup, attach/detach on a file.
* ``discovery`` — read-only questions: collisions, what a selection carries,
  what a tag touches.
* ``sharing`` — grants to specific users and groups (``v386_add_tag_share``).
* ``operations`` — the destructive half: rename, merge, delete, promote.

``_common`` holds the shared imports, the three scope predicates and the error
translation, so no module re-derives them.

**Include order is load-bearing.** ``discovery`` registers the literal
``/for-files``, and ``operations`` registers ``/impact`` and ``/promote``;
FastAPI matches in registration order, so both must come before ``sharing`` and
the other ``/{tag_uuid}`` routes or the literal paths are swallowed by the path
parameter. ``crud`` is first because ``GET ""``/``POST ""`` cannot collide with
anything.
"""

# Imported for their registration side effects, in this order. Every module
# registers onto the one router in `_common`; sub-routers were tried and
# rejected because FastAPI refuses an empty path when including a router with
# no prefix, and `POST ""` / `GET ""` are real routes here.
from app.api.endpoints.tags import crud  # noqa: E402,F401
from app.api.endpoints.tags import discovery  # noqa: E402,F401
from app.api.endpoints.tags import operations  # noqa: E402,F401
from app.api.endpoints.tags import sharing  # noqa: E402,F401
from app.api.endpoints.tags._common import router

__all__ = ["router"]
