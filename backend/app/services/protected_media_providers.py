"""Protected media providers for authenticated media downloads.

This module defines a simple plugin-style registry for sites that require
authentication before media can be downloaded.

Each provider implements a small interface for:
- deciding whether it can handle a given URL
- extracting basic media metadata without downloading
- downloading the original media file to a local directory
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable
from typing import Any
from typing import Protocol

logger = logging.getLogger(__name__)


class ProtectedMediaProvider(Protocol):
    """Interface for a protected media provider.

    Providers are responsible for handling URLs from specific hosts that
    require authentication and cannot be processed by yt-dlp alone.
    """

    def can_handle(self, url: str, user_id: int | None = None) -> bool:
        """Return True if this provider knows how to handle the given URL."""

    def extract_info(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Return a yt-dlp-like info dict for the media without downloading.

        The returned dict should contain at least:
        - id
        - title
        - duration (optional)
        - description (optional)
        - uploader (optional)
        - extractor (short provider name)
        """

    def download(
        self,
        url: str,
        output_path: str,
        progress_callback: Callable[[int, str], None] | None = None,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Download media file for this URL into output_path.

        Must return a dict with keys:
        - file_path: absolute path to downloaded file
        - filename: basename of the downloaded file
        - info: the same kind of dict as extract_info() returns
        """

    def get_public_auth_config(self) -> dict[str, Any]:
        """Return public auth config for this provider.

        Expected shape (per host or combined):
        {
          "hosts": ["media.example.com"],
          "auth_type": "user_password" | "api_key" | "browser_cookies" | ...,
          "fields": [
            {"name": "media_username", "label": "Media username", "type": "text"},
            {"name": "media_password", "label": "Media password", "type": "password"},
          ],
        }

        Providers that do not wish to expose configuration publicly can
        return an empty dict.
        """


def _load_providers() -> list[ProtectedMediaProvider]:
    """Dynamically load all protected media providers from the plugins package.

    Plugins live under ``app.services.protected_media_plugins`` and should
    expose either:
      - a module-level variable ``provider`` implementing ProtectedMediaProvider
      - or a callable ``get_provider()`` returning such an instance
    """
    providers: list[ProtectedMediaProvider] = []

    try:
        import app.services.protected_media_plugins as plugin_pkg  # type: ignore[import]
    except ImportError:
        logger.info("No protected media plugins package found")
        return providers

    package_path = getattr(plugin_pkg, "__path__", None)
    if package_path is None:
        logger.warning("protected_media_plugins has no __path__; skipping plugin discovery")
        return providers

    prefix = plugin_pkg.__name__ + "."

    for module_info in pkgutil.iter_modules(package_path, prefix):
        try:
            module = importlib.import_module(module_info.name)
        except Exception as e:
            logger.warning(f"Failed to import protected media plugin {module_info.name}: {e}")
            continue

        # Convention 1: module-level variable `provider`
        plugin = getattr(module, "provider", None)
        if plugin is not None:
            providers.append(plugin)
            continue

        # Convention 2: callable `get_provider()`
        get_provider = getattr(module, "get_provider", None)
        if callable(get_provider):
            try:
                provider_instance = get_provider()
                providers.append(provider_instance)
            except Exception as e:
                logger.warning(f"get_provider() failed in {module_info.name}: {e}")

    return providers


# Registry of all protected media providers (loaded at import time)
PROTECTED_MEDIA_PROVIDERS: list[ProtectedMediaProvider] = _load_providers()


def get_protected_media_auth_config() -> list[dict[str, Any]]:
    """Aggregate public auth config for all protected media providers.

    Each entry is expected to have at minimum:
      - hosts: list of hostnames
      - auth_type: short string describing auth mechanism
      - fields: optional list of field descriptors for UI
    """
    configs: list[dict[str, Any]] = []

    for provider in PROTECTED_MEDIA_PROVIDERS:
        get_config = getattr(provider, "get_public_auth_config", None)
        if not callable(get_config):
            continue
        try:
            cfg = get_config()
            if cfg:
                configs.append(cfg)
        except Exception as e:
            logger.warning(
                f"get_public_auth_config() failed for {provider.__class__.__name__}: {e}"
            )

    return configs
