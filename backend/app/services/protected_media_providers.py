"""Registry of protected-media providers: download sources that need a site login.

yt-dlp covers public platforms. Some sites (for example a self-hosted MediaCMS) only
serve media to a signed-in user, so each such site is supported by a provider plugin in
``app.services.protected_media_plugins``. This module defines the interface those plugins
implement, discovers them once at import time, and assembles the non-secret login-form
description the frontend uses to decide when to ask for site credentials.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from collections.abc import Callable
from typing import Any
from typing import Protocol

logger = logging.getLogger(__name__)

_PLUGIN_PACKAGE = "app.services.protected_media_plugins"


class ProtectedMediaProvider(Protocol):
    """What a protected-media plugin must offer.

    A provider decides which URLs it is responsible for, signs in to the site on the
    user's behalf, and either describes or fetches the media. Every method receives the
    requesting user's id so a provider can consult that user's own media sources.
    """

    def can_handle(self, url: str, user_id: int | None = None) -> bool:
        """Report whether this provider is responsible for ``url``.

        Args:
            url: The media URL the user submitted.
            user_id: The requesting user, if known.

        Returns:
            True when this provider should handle the URL instead of yt-dlp.
        """
        ...

    def extract_info(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Describe the media at ``url`` without downloading it.

        Args:
            url: The media URL.
            username: Site username given with this request, if any.
            password: Site password given with this request, if any.
            user_id: The requesting user, if known.

        Returns:
            A metadata dict shaped like yt-dlp's: always ``id``, ``title`` and
            ``extractor``; ``duration``, ``description`` and ``uploader`` when known.
        """
        ...

    def download(
        self,
        url: str,
        output_path: str,
        progress_callback: Callable[[int, str], None] | None = None,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Fetch the media at ``url`` into the directory ``output_path``.

        Args:
            url: The media URL.
            output_path: Directory the file is written into.
            progress_callback: Optional ``(percent, message)`` reporter.
            username: Site username given with this request, if any.
            password: Site password given with this request, if any.
            user_id: The requesting user, if known.

        Returns:
            ``{"file_path": ..., "filename": ..., "info": <metadata as from extract_info>}``.
        """
        ...

    def get_public_auth_config(self, user_id: int | None = None) -> dict[str, Any]:
        """Describe, without any secret, the login form this provider needs.

        Args:
            user_id: The user the description is for; hosts depend on their sources.

        Returns:
            ``{"hosts": [...], "auth_type": str, "fields": [{"name", "label", "type"}, ...]}``,
            or an empty dict when the provider has nothing to offer this user.
        """
        ...


def _provider_from_module(module: Any) -> ProtectedMediaProvider | None:
    """Return the provider a plugin module registers, or None if it registers none.

    A module-level ``provider`` object takes precedence; ``get_provider`` is then never
    called. Otherwise a callable ``get_provider`` is invoked to build one.
    """
    registered: ProtectedMediaProvider | None = getattr(module, "provider", None)
    if registered is not None:
        return registered
    factory = getattr(module, "get_provider", None)
    if callable(factory):
        built: ProtectedMediaProvider | None = factory()
        return built
    return None


def _load_providers() -> list[ProtectedMediaProvider]:
    """Import every plugin module and collect the providers they register.

    A plugin that fails to import or to build its provider is logged and left out, so
    one broken plugin never takes the others down.

    Returns:
        The providers, in the order their modules were discovered.
    """
    try:
        package = importlib.import_module(_PLUGIN_PACKAGE)
    except ImportError as exc:
        logger.info("No protected-media plugin package available: %s", exc)
        return []

    search_path = getattr(package, "__path__", None)
    if search_path is None:
        logger.warning("%s is not a package; no protected-media plugins loaded", _PLUGIN_PACKAGE)
        return []

    providers: list[ProtectedMediaProvider] = []
    for module_info in pkgutil.iter_modules(search_path, _PLUGIN_PACKAGE + "."):
        try:
            module = importlib.import_module(module_info.name)
        except (
            Exception
        ) as exc:  # boundary: third-party plugin isolation, a bad plugin must not stop the rest
            logger.warning(
                "Skipping protected-media plugin %s: import failed: %s",
                module_info.name,
                exc,
                exc_info=True,
            )
            continue
        try:
            provider = _provider_from_module(module)
        except Exception as exc:  # boundary: third-party plugin isolation
            logger.warning(
                "Skipping protected-media plugin %s: get_provider() failed: %s",
                module_info.name,
                exc,
                exc_info=True,
            )
            continue
        if provider is not None:
            providers.append(provider)
    return providers


PROTECTED_MEDIA_PROVIDERS: list[ProtectedMediaProvider] = _load_providers()


def _accepts_user_id(method: Callable[..., Any]) -> bool:
    """True when ``method`` declares a ``user_id`` parameter (older plugins do not)."""
    return "user_id" in inspect.signature(method).parameters


def get_protected_media_auth_config(user_id: int | None = None) -> list[dict[str, Any]]:
    """Collect every provider's public login-form description for one user.

    Args:
        user_id: The user asking; providers resolve the hosts that apply to them.

    Returns:
        One non-empty description per provider that offers one, in registry order.
        A provider that fails is logged and skipped.
    """
    configs: list[dict[str, Any]] = []
    for provider in PROTECTED_MEDIA_PROVIDERS:
        describe = getattr(provider, "get_public_auth_config", None)
        if not callable(describe):
            continue
        try:
            config = describe(user_id=user_id) if _accepts_user_id(describe) else describe()
        except Exception as exc:  # boundary: third-party plugin isolation
            logger.warning(
                "Protected-media provider %s failed in get_public_auth_config: %s",
                type(provider).__name__,
                exc,
                exc_info=True,
            )
            continue
        if config:
            configs.append(config)
    return configs
