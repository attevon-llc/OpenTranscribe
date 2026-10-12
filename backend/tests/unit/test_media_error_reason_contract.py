"""GH #960 — backend `UserErrorReason` <-> frontend `MediaErrorReason` contract.

Modeled on `test_chat_sse_contract.py` (#611/#964): parse BOTH real source files rather
than hand-copying either set, so this fails the moment the two sides drift instead of
relying on someone remembering to update both.

What this would catch: a backend engineer adding a ninth `UserErrorReason` member without
touching the frontend. TypeScript's `Record<MediaErrorReason, string>` in `mediaErrors.ts`
already refuses to COMPILE in that situation (no index signature — see that file's
docstring) — this test is the same invariant checked from the backend side, in the suite a
backend-only change is most likely to run, and it also catches the reverse drift (a stale
frontend member with no backend counterpart) that a one-directional TS check cannot.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from app.services.error_categorization_service import UserErrorReason

pytestmark = pytest.mark.unit

_BACKEND_ROOT = Path(__file__).resolve().parents[2] / "app"
_FRONTEND_SRC = Path(__file__).resolve().parents[3] / "frontend" / "src"

_MEDIA_ERRORS_TS = _FRONTEND_SRC / "lib" / "i18n" / "mediaErrors.ts"
_MEDIA_TYPES_TS = _FRONTEND_SRC / "lib" / "types" / "media.ts"
_EN_LOCALE = _FRONTEND_SRC / "lib" / "i18n" / "locales" / "en.json"


def backend_user_error_reasons() -> set[str]:
    """The live `UserErrorReason` enum values, read from the real enum — not transcribed."""
    return {member.value for member in UserErrorReason}


def frontend_media_error_reason_union() -> set[str]:
    """The `MediaErrorReason` union members in `types/media.ts`."""
    source = _MEDIA_TYPES_TS.read_text(encoding="utf-8")
    match = re.search(r"export type MediaErrorReason =\s*((?:\s*\|\s*'[^']+')+)", source)
    assert match is not None, "MediaErrorReason union not found in media.ts — did it move?"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def frontend_media_error_i18n_keys() -> dict[str, str]:
    """The `MEDIA_ERROR_REASON_I18N_KEY` map in `mediaErrors.ts`, reason -> i18n key."""
    source = _MEDIA_ERRORS_TS.read_text(encoding="utf-8")
    match = re.search(
        r"MEDIA_ERROR_REASON_I18N_KEY:\s*Record<MediaErrorReason,\s*string>\s*=\s*\{(.*?)\};",
        source,
        re.DOTALL,
    )
    assert match is not None, "MEDIA_ERROR_REASON_I18N_KEY map not found in mediaErrors.ts"
    pairs = re.findall(r"(\w+):\s*'([^']+)'", match.group(1))
    return dict(pairs)


class TestBackendFrontendReasonParity:
    def test_backend_enum_matches_frontend_type_union(self):
        backend = backend_user_error_reasons()
        frontend = frontend_media_error_reason_union()

        assert backend == frontend, (
            f"backend UserErrorReason and frontend MediaErrorReason drifted: "
            f"backend-only={backend - frontend}, frontend-only={frontend - backend}"
        )

    def test_every_backend_reason_has_a_frontend_i18n_mapping(self):
        backend = backend_user_error_reasons()
        mapped = set(frontend_media_error_i18n_keys())

        assert backend == mapped, (
            f"backend UserErrorReason and mediaErrors.ts's map drifted: "
            f"unmapped={backend - mapped}, orphaned={mapped - backend}"
        )

    def test_every_mapped_i18n_key_exists_in_the_english_locale(self):
        """The must-see-red case from the issue: a reason with no translated copy must not
        render a raw dot-notation key. `en.json` is the reference locale `check:i18n`
        enforces parity against."""
        en = json.loads(_EN_LOCALE.read_text(encoding="utf-8"))
        keys = frontend_media_error_i18n_keys()

        missing = {reason: key for reason, key in keys.items() if key not in en}
        assert not missing, f"i18n keys with no en.json entry: {missing}"

    def test_backend_enum_is_a_closed_ast_walkable_set(self):
        """Sanity check on the extraction itself: parse `error_categorization_service.py`'s
        `UserErrorReason` class body directly, independent of importing the enum, so a
        change to how the enum is defined (e.g. swapping StrEnum for a plain str subclass)
        cannot silently make `backend_user_error_reasons()` under-count."""
        source = (_BACKEND_ROOT / "services" / "error_categorization_service.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        class_node = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "UserErrorReason"
        )
        values = {
            stmt.value.value
            for stmt in class_node.body
            if isinstance(stmt, ast.Assign)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        }
        assert values == backend_user_error_reasons()
