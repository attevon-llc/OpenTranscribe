"""Structural-conformance tests for ``app/services/interfaces.py``.

``interfaces.py`` declares four ``Protocol`` contracts (``StorageService``,
``SearchService``, ``CacheService``, ``NotificationService``) that existing
concrete implementations satisfy purely by matching method signatures -- no
inheritance, no decorator. None of the four is ``@runtime_checkable`` (unlike
``app/services/documents/protocol.py::DocumentParser``), so ``isinstance``
cannot be used to check conformance here. Instead these tests compare
``inspect.signature`` of each protocol method against the real implementation
it documents itself as mirroring, the same thing a human reviewer would check
by eye -- and the same thing that silently drifts when one side is renamed
without the other.

A parameter rename, reorder, or default-value change on either side makes
these tests fail, which is the point: the docstrings in ``interfaces.py``
assert "matches X"; these tests are what makes that assertion falsifiable.
"""

from __future__ import annotations

import inspect

from app.services.interfaces import CacheService
from app.services.interfaces import NotificationService
from app.services.interfaces import SearchService
from app.services.interfaces import StorageService


def _protocol_method_names(cls: type) -> set[str]:
    """Public callables declared directly on a Protocol class body."""
    return {
        name for name, value in vars(cls).items() if not name.startswith("_") and callable(value)
    }


def _params(fn) -> list[str]:
    """Parameter names of a function/method, with ``self`` dropped."""
    names = list(inspect.signature(fn).parameters.keys())
    if names and names[0] == "self":
        names = names[1:]
    return names


def _defaults(fn) -> dict[str, object]:
    """Map of parameter name -> default, for parameters that have one."""
    sig = inspect.signature(fn)
    return {
        name: p.default
        for name, p in sig.parameters.items()
        if p.default is not inspect.Parameter.empty
    }


class TestProtocolsAreProtocols:
    """Each contract is actually a ``typing.Protocol``, not a plain class.

    ``typing.Protocol.__init_subclass__`` stamps ``_is_protocol = True`` on
    every direct Protocol subclass; checking that (rather than
    ``Protocol in cls.__mro__``) avoids mypy's "non-overlapping container
    check" on a special form living inside a ``tuple[type, ...]``.
    """

    def test_storage_service_is_a_protocol(self):
        assert getattr(StorageService, "_is_protocol", False) is True

    def test_search_service_is_a_protocol(self):
        assert getattr(SearchService, "_is_protocol", False) is True

    def test_cache_service_is_a_protocol(self):
        assert getattr(CacheService, "_is_protocol", False) is True

    def test_notification_service_is_a_protocol(self):
        assert getattr(NotificationService, "_is_protocol", False) is True


class TestDeclaredSurface:
    """The exact method set each Protocol declares -- pins the contract."""

    def test_storage_service_declares_exactly_these_methods(self):
        assert _protocol_method_names(StorageService) == {
            "upload_file",
            "download_file",
            "get_presigned_url",
            "delete_object",
        }

    def test_search_service_declares_exactly_these_methods(self):
        assert _protocol_method_names(SearchService) == {
            "index_transcript",
            "remove_speaker_embedding",
        }

    def test_cache_service_declares_exactly_these_methods(self):
        assert _protocol_method_names(CacheService) == {"get", "set", "delete_pattern"}

    def test_notification_service_declares_exactly_these_methods(self):
        assert _protocol_method_names(NotificationService) == {"send"}


class TestStorageServiceMatchesMinIOService:
    """``StorageService`` documents itself as mirroring ``MinIOService``."""

    def test_upload_file_signature_matches(self):
        from app.services.minio_service import MinIOService

        assert _params(StorageService.upload_file) == _params(MinIOService.upload_file)
        assert _defaults(StorageService.upload_file) == _defaults(MinIOService.upload_file)

    def test_download_file_signature_matches(self):
        from app.services.minio_service import MinIOService

        assert _params(StorageService.download_file) == _params(MinIOService.download_file)
        assert _defaults(StorageService.download_file) == _defaults(MinIOService.download_file)

    def test_get_presigned_url_signature_matches(self):
        from app.services.minio_service import MinIOService

        assert _params(StorageService.get_presigned_url) == _params(MinIOService.get_presigned_url)
        assert _defaults(StorageService.get_presigned_url) == _defaults(
            MinIOService.get_presigned_url
        )

    def test_delete_object_signature_matches(self):
        from app.services.minio_service import MinIOService

        assert _params(StorageService.delete_object) == _params(MinIOService.delete_object)


class TestSearchServiceMatchesOpenSearchModuleFunctions:
    """``SearchService`` documents itself as mirroring the module-level
    ``opensearch_service`` functions (now a package, re-exported unchanged)."""

    def test_index_transcript_signature_matches(self):
        from app.services.opensearch_service import index_transcript

        assert _params(SearchService.index_transcript) == _params(index_transcript)
        assert _defaults(SearchService.index_transcript) == _defaults(index_transcript)

    def test_remove_speaker_embedding_signature_matches(self):
        from app.services.opensearch_service import remove_speaker_embedding

        assert _params(SearchService.remove_speaker_embedding) == _params(remove_speaker_embedding)


class TestCacheServiceMatchesRedisCacheService:
    """``CacheService`` documents itself as mirroring ``RedisCacheService``."""

    def test_get_signature_matches(self):
        from app.services.redis_cache_service import RedisCacheService

        assert _params(CacheService.get) == _params(RedisCacheService.get)

    def test_set_signature_matches(self):
        from app.services.redis_cache_service import RedisCacheService

        assert _params(CacheService.set) == _params(RedisCacheService.set)
        assert _defaults(CacheService.set) == _defaults(RedisCacheService.set)

    def test_delete_pattern_signature_matches(self):
        from app.services.redis_cache_service import RedisCacheService

        assert _params(CacheService.delete_pattern) == _params(RedisCacheService.delete_pattern)


class TestNotificationServiceMatchesSendWsEvent:
    """``NotificationService.send`` documents itself as mirroring the free
    function ``send_ws_event`` -- there is no class-based implementation, so
    the comparison is against a plain function (no ``self`` to strip on
    either side, since ``_params`` only drops a leading ``self``)."""

    def test_send_signature_matches_send_ws_event(self):
        from app.utils.websocket_notify import send_ws_event

        assert _params(NotificationService.send) == _params(send_ws_event)

    def test_send_return_annotation_matches(self):
        from app.utils.websocket_notify import send_ws_event

        # interfaces.py has `from __future__ import annotations`, so a plain
        # inspect.signature() would return the unevaluated string "bool" for
        # the protocol method but the real `bool` type for send_ws_event.
        # eval_str=True resolves both sides the same way.
        protocol_sig = inspect.signature(NotificationService.send, eval_str=True)
        real_sig = inspect.signature(send_ws_event, eval_str=True)
        assert protocol_sig.return_annotation is bool
        assert real_sig.return_annotation is bool
