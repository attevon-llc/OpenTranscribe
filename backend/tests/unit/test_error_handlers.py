"""Real behavioral tests for ``app/utils/error_handlers.py`` (issue #474).

A decorator (``handle_not_found``) and a static-method builder class (``ErrorHandler``).
No DB, no network.

``handle_database_errors`` was deleted (#914 STEP 6, issue #431 grep confirmed zero
call sites under ``app/`` -- its only importer anywhere was this test file). It caught
a plain ``except Exception`` around the wrapped function, which reclassified any
``HTTPException`` the wrapped function raised into an opaque 500 -- the exact
passthrough defect ``test_http_exception_passthrough.py`` exists to catch at other
call sites. Its 12 test cases (the two Success/SQLAlchemyError/GenericException/Logging
classes) went with it.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException

from app.utils.error_handlers import ErrorHandler
from app.utils.error_handlers import handle_not_found


class TestHandleNotFound:
    def test_returns_the_result_unchanged_when_not_none(self):
        @handle_not_found("Widget")
        def get_widget():
            return {"id": 1}

        assert get_widget() == {"id": 1}

    def test_raises_404_with_the_default_resource_name_when_result_is_none(self):
        @handle_not_found()
        def get_missing():
            return None

        with pytest.raises(HTTPException) as excinfo:
            get_missing()
        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Resource not found"

    def test_raises_404_with_a_custom_resource_name(self):
        @handle_not_found("Transcript")
        def get_missing():
            return None

        with pytest.raises(HTTPException) as excinfo:
            get_missing()
        assert excinfo.value.detail == "Transcript not found"

    def test_a_falsy_but_non_none_result_is_not_treated_as_missing(self):
        # The check is `result is None`, not truthiness — an empty list/dict/0/""
        # must all pass through untouched.
        falsy_values: list[object] = [[], {}, 0, "", False]
        for falsy in falsy_values:

            @handle_not_found("Thing")
            def get_falsy(_v=falsy):
                return _v

            assert get_falsy() == falsy

    def test_passes_through_args_and_kwargs_to_the_wrapped_function(self):
        @handle_not_found("Item")
        def get_item(item_id, *, suffix=""):
            if item_id == 0:
                return None
            return f"item-{item_id}{suffix}"

        assert get_item(5, suffix="-x") == "item-5-x"
        with pytest.raises(HTTPException):
            get_item(0)

    def test_decorator_factory_returns_a_reusable_decorator(self):
        # handle_not_found() itself is a factory; the returned decorator must be
        # usable on more than one function without cross-talk.
        decorator = handle_not_found("Shared")

        @decorator
        def a():
            return None

        @decorator
        def b():
            return "ok"

        with pytest.raises(HTTPException):
            a()
        assert b() == "ok"


class TestErrorHandlerDatabaseError:
    def test_builds_a_500_with_the_operation_in_the_detail(self):
        exc = ErrorHandler.database_error("saving user", ValueError("boom"))
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 500
        assert exc.detail == "Database error during saving user"

    def test_logs_the_original_error_text(self, caplog):
        with caplog.at_level(logging.ERROR, logger="app.utils.error_handlers"):
            ErrorHandler.database_error("deleting file", RuntimeError("disk full"))
        assert any("disk full" in record.message for record in caplog.records)


class TestErrorHandlerValidationError:
    def test_builds_a_400_with_the_message_verbatim(self):
        exc = ErrorHandler.validation_error("email is required")
        assert exc.status_code == 400
        assert exc.detail == "email is required"

    def test_empty_message_is_preserved_as_is(self):
        exc = ErrorHandler.validation_error("")
        assert exc.detail == ""

    def test_unicode_message_is_preserved_byte_for_byte(self):
        msg = "文件名无效 — 🚫"
        exc = ErrorHandler.validation_error(msg)
        assert exc.detail == msg


class TestErrorHandlerNotFoundError:
    def test_builds_a_404_with_the_resource_name_suffixed(self):
        exc = ErrorHandler.not_found_error("Speaker")
        assert exc.status_code == 404
        assert exc.detail == "Speaker not found"


class TestErrorHandlerUnauthorizedError:
    def test_default_message_is_access_denied(self):
        exc = ErrorHandler.unauthorized_error()
        assert exc.status_code == 403
        assert exc.detail == "Access denied"

    def test_custom_message_overrides_the_default(self):
        exc = ErrorHandler.unauthorized_error("You cannot edit this recording")
        assert exc.status_code == 403
        assert exc.detail == "You cannot edit this recording"


class TestErrorHandlerInternalError:
    def test_default_message_is_generic(self):
        exc = ErrorHandler.internal_error()
        assert exc.status_code == 500
        assert exc.detail == "Internal server error"

    def test_custom_message_overrides_the_default(self):
        exc = ErrorHandler.internal_error("Redaction service unavailable")
        assert exc.status_code == 500
        assert exc.detail == "Redaction service unavailable"


class TestErrorHandlerFileProcessingError:
    def test_builds_a_500_with_the_operation_in_the_detail(self):
        exc = ErrorHandler.file_processing_error("transcoding", OSError("no space left"))
        assert exc.status_code == 500
        assert exc.detail == "File processing failed during transcoding"

    def test_logs_the_original_error_text(self, caplog):
        with caplog.at_level(logging.ERROR, logger="app.utils.error_handlers"):
            ErrorHandler.file_processing_error("extracting audio", OSError("no space left"))
        assert any("no space left" in record.message for record in caplog.records)
