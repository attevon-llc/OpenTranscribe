"""Real behavioral tests for ``app/utils/error_handlers.py`` (issue #474).

Two decorators (``handle_database_errors``, ``handle_not_found``) and a static-method
builder class (``ErrorHandler``) with zero prior test coverage. No DB, no network — the
only "external" thing touched is a real ``sqlalchemy.orm.Session`` bound to an in-memory
SQLite engine, used to prove ``handle_database_errors`` actually calls ``.rollback()``
on it rather than merely not crashing.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.utils.error_handlers import ErrorHandler
from app.utils.error_handlers import handle_database_errors
from app.utils.error_handlers import handle_not_found


def _sqlite_session() -> Session:
    engine = create_engine("sqlite://")
    return sessionmaker(bind=engine)()


class TestHandleDatabaseErrorsSuccess:
    def test_passes_through_the_return_value_untouched(self):
        @handle_database_errors
        def op(x, y):
            return x + y

        assert op(2, 3) == 5

    def test_preserves_the_wrapped_functions_name(self):
        @handle_database_errors
        def my_named_operation():
            return None

        assert my_named_operation.__name__ == "my_named_operation"


class TestHandleDatabaseErrorsSQLAlchemyError:
    def test_raises_500_with_the_fixed_detail_message(self):
        @handle_database_errors
        def op():
            raise SQLAlchemyError("connection lost")

        with pytest.raises(HTTPException) as excinfo:
            op()
        assert excinfo.value.status_code == 500
        assert excinfo.value.detail == "Database operation failed"

    def test_chains_the_original_exception_as_the_cause(self):
        original = SQLAlchemyError("boom")

        @handle_database_errors
        def op():
            raise original

        with pytest.raises(HTTPException) as excinfo:
            op()
        assert excinfo.value.__cause__ is original

    def test_rolls_back_a_real_session_passed_as_a_db_kwarg(self):
        session = _sqlite_session()
        rollback_calls: list[bool] = []
        real_rollback = session.rollback

        def spy_rollback():
            rollback_calls.append(True)
            return real_rollback()

        session.rollback = spy_rollback  # type: ignore[method-assign]

        @handle_database_errors
        def op(*, db):
            raise SQLAlchemyError("write failed")

        with pytest.raises(HTTPException):
            op(db=session)

        assert rollback_calls == [True]
        session.close()

    def test_does_not_roll_back_when_db_kwarg_is_not_a_session_instance(self):
        # A plain object under key "db" must not blow up the error path with an
        # AttributeError from calling .rollback() on something that has none.
        @handle_database_errors
        def op(*, db):
            raise SQLAlchemyError("write failed")

        with pytest.raises(HTTPException) as excinfo:
            op(db={"not": "a session"})
        assert excinfo.value.status_code == 500

    def test_does_not_require_a_db_kwarg_at_all(self):
        @handle_database_errors
        def op():
            raise SQLAlchemyError("write failed")

        with pytest.raises(HTTPException) as excinfo:
            op()
        assert excinfo.value.status_code == 500

    def test_a_db_passed_positionally_is_not_rolled_back(self):
        # Documented behavior: the docstring says "if available in kwargs" — a
        # positional db is invisible to the check. This pins that contract rather
        # than a bug: the decorator inspects **kwargs only.
        session = _sqlite_session()
        rollback_calls: list[bool] = []
        session.rollback = lambda: rollback_calls.append(True)  # type: ignore[method-assign]

        @handle_database_errors
        def op(db):
            raise SQLAlchemyError("write failed")

        with pytest.raises(HTTPException):
            op(session)

        assert rollback_calls == []
        session.close()


class TestHandleDatabaseErrorsGenericException:
    def test_raises_500_with_the_generic_detail_message(self):
        @handle_database_errors
        def op():
            raise ValueError("something else broke")

        with pytest.raises(HTTPException) as excinfo:
            op()
        assert excinfo.value.status_code == 500
        assert excinfo.value.detail == "An unexpected error occurred"

    def test_generic_exception_path_never_touches_db_rollback(self):
        # A non-SQLAlchemyError must fall into the second except clause, which has
        # no rollback logic at all — a db kwarg present must not change the message.
        @handle_database_errors
        def op(*, db):
            raise ValueError("boom")

        with pytest.raises(HTTPException) as excinfo:
            op(db=_sqlite_session())
        assert excinfo.value.detail == "An unexpected error occurred"

    def test_an_httpexception_raised_by_the_wrapped_function_is_reclassified(self):
        # HTTPException is a subclass of Exception, not SQLAlchemyError, so it falls
        # into the generic branch and comes back OPAQUE (500, generic detail) rather
        # than passing the original status/detail through untouched.
        @handle_database_errors
        def op():
            raise HTTPException(status_code=404, detail="not found")

        with pytest.raises(HTTPException) as excinfo:
            op()
        assert excinfo.value.status_code == 500
        assert excinfo.value.detail == "An unexpected error occurred"


class TestHandleDatabaseErrorsLogging:
    def test_a_database_error_is_logged_at_error_level_with_the_function_name(self, caplog):
        @handle_database_errors
        def named_op():
            raise SQLAlchemyError("db is down")

        with caplog.at_level(logging.ERROR, logger="app.utils.error_handlers"):
            with pytest.raises(HTTPException):
                named_op()

        assert any(
            "named_op" in record.message and "db is down" in record.message
            for record in caplog.records
        )


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
