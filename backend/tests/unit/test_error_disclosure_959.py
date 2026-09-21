"""GH #959 — the five #786 residuals.

Item 1 (raw exception no longer stored) and item 2 (retry keys off a persisted code, never
re-parsed prose) are one piece of work — see the module docstring on
`app/utils/error_classification.py::resolve_persisted_error_category` for why. These tests
pin both halves as pure-unit behaviour (no DB needed): `ErrorCategorizationService.
sanitize_for_storage` (what is now safe to persist) and `resolve_persisted_error_category`
(what retry recovery reads).

DB-backed coverage of `update_task_status`'s chokepoint sanitization
(`app/utils/task_utils.py`) and of `task_recovery_service.py`'s two call sites lives beside
the existing DB-backed suites for those modules
(`tests/unit/test_pipeline_task_terminus.py`, `tests/unit/test_task_recovery_service.py`) —
this file does not duplicate a DB fixture.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from app.services.error_categorization_service import ErrorCategorizationService
from app.services.error_categorization_service import UserErrorReason
from app.utils.error_classification import ErrorCategory
from app.utils.error_classification import resolve_persisted_error_category

pytestmark = pytest.mark.unit

SENTINEL = "SENTINEL-/srv/internal/traceback/line/42"

_AUDIT_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "audit-error-disclosure.py"


def _load_audit_module() -> ModuleType:
    """Load `scripts/audit-error-disclosure.py` by path — its filename has hyphens, so it
    is not importable as `audit_error_disclosure` without this.

    The module must be registered in `sys.modules` BEFORE `exec_module` runs: the script's
    `@dataclass(frozen=True) class Finding` resolves its module via
    `sys.modules[cls.__module__]` while dataclass decoration is still executing, and that
    lookup raises `AttributeError` on a module object that isn't registered yet.
    """
    import sys

    spec = importlib.util.spec_from_file_location("audit_error_disclosure", _AUDIT_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestSanitizeForStorage:
    """`ErrorCategorizationService.sanitize_for_storage` — GH #959 item 1."""

    def test_never_returns_the_raw_text(self):
        raw = f"CUDA out of memory while allocating tensor at {SENTINEL}"
        sanitized = ErrorCategorizationService.sanitize_for_storage(raw)

        assert SENTINEL not in sanitized
        assert raw != sanitized

    def test_classifies_correctly_despite_sanitizing(self):
        raw = f"This file is corrupted and cannot be decoded: {SENTINEL}"
        sanitized = ErrorCategorizationService.sanitize_for_storage(raw)

        info = ErrorCategorizationService.get_error_info(raw)
        assert info["category"] == UserErrorReason.FILE_QUALITY.value
        assert sanitized == info["user_message"]

    def test_none_and_empty_are_safe(self):
        assert ErrorCategorizationService.sanitize_for_storage(None)
        assert ErrorCategorizationService.sanitize_for_storage("")

    def test_idempotent_on_an_already_sanitized_message(self):
        """A message this function already produced must not blow up when re-run —
        `task_recovery_service`'s read paths and `management.py`'s debug endpoint both do
        this on legacy rows."""
        once = ErrorCategorizationService.sanitize_for_storage("network timeout occurred")
        twice = ErrorCategorizationService.sanitize_for_storage(once)

        assert isinstance(twice, str)
        assert twice  # never empty


class TestBuildErrorResponseFields:
    """GH #959 item 4 — the crud.py/formatting_service.py dedup."""

    def test_shape_matches_the_four_wire_fields(self):
        fields = ErrorCategorizationService.build_error_response_fields("no audio track found")

        assert set(fields) == {
            "error_reason",
            "error_suggestions",
            "user_message",
            "is_retryable",
        }
        assert fields["error_reason"] == UserErrorReason.NO_AUDIO_TRACK.value


class TestResolvePersistedErrorCategory:
    """GH #959 item 2 — retry keys off the persisted code, never re-parsed prose."""

    def test_prefers_the_persisted_category_over_reparsing(self):
        # The stored message no longer contains any classifiable keyword (it is the
        # #959 fixed sentence) — a naive re-parse would misclassify this as UNKNOWN.
        stored_category = ErrorCategory.OOM_ERROR.value
        sanitized_message = "Processing failed for this file."

        resolved = resolve_persisted_error_category(stored_category, sanitized_message)

        assert resolved == ErrorCategory.OOM_ERROR

    def test_falls_back_to_reparsing_only_when_no_category_was_ever_persisted(self):
        resolved = resolve_persisted_error_category(None, "connection timeout occurred")

        assert resolved == ErrorCategory.NETWORK_ERROR

    def test_falls_back_on_an_empty_string_category(self):
        resolved = resolve_persisted_error_category("", "out of memory")

        assert resolved == ErrorCategory.OOM_ERROR

    def test_an_unrecognized_persisted_value_falls_back_rather_than_raising(self):
        resolved = resolve_persisted_error_category("not-a-real-category", "network timeout")

        assert resolved == ErrorCategory.NETWORK_ERROR

    def test_no_information_at_all_resolves_to_unknown(self):
        resolved = resolve_persisted_error_category(None, None)

        assert resolved == ErrorCategory.UNKNOWN


class TestAuditErrorDisclosureGate:
    """GH #959 item 5 — the scanner itself, exercised as a library so a regression in the
    detector is caught here rather than only by its own `--list` output."""

    def test_flags_an_fstring_interpolating_the_exception_variable(self, tmp_path):
        audit = _load_audit_module()

        bad = tmp_path / "bad_site.py"
        bad.write_text(
            'def handler(media_file, e):\n    media_file.last_error_message = f"boom: {e}"\n'
        )
        findings = audit.scan([bad])
        assert len(findings) == 1
        assert findings[0].field == "last_error_message"

    def test_does_not_flag_a_sanitized_assignment(self, tmp_path):
        audit = _load_audit_module()

        good = tmp_path / "good_site.py"
        good.write_text(
            "def handler(media_file, e):\n"
            "    media_file.last_error_message = "
            "ErrorCategorizationService.sanitize_for_storage(str(e))\n"
        )
        findings = audit.scan([good])
        assert findings == []
