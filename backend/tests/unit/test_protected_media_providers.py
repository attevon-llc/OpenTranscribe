"""Real behavioral tests for ``app/services/protected_media_providers.py`` (issue #474).

Two things with zero prior coverage:

1. ``_load_providers()`` — the ``pkgutil``-based plugin discovery that scans
   ``app.services.protected_media_plugins`` for the two supported conventions
   (module-level ``provider``, or callable ``get_provider()``) and must never let one
   broken plugin module take the whole registry down.
2. ``get_protected_media_auth_config()`` — the aggregator that walks
   ``PROTECTED_MEDIA_PROVIDERS`` and must skip a provider that has no (callable)
   ``get_public_auth_config``, drop an empty config, and swallow a provider that raises.

``_load_providers`` is exercised against a REAL temporary plugin package on disk (real
``.py`` files, real ``pkgutil.iter_modules`` + ``importlib.import_module``) rather than a
mocked ``pkgutil`` — that is the only way to prove the dynamic-loading contract actually
works end to end. The aggregator tests use plain duck-typed objects (not ``unittest.mock``)
standing in for providers, since the module itself never does an ``isinstance`` check
against the ``Protocol``.
"""

from __future__ import annotations

import sys
import types

import pytest

from app.services import protected_media_providers as pmp

PKG_NAME = "app.services.protected_media_plugins"


def _install_fake_plugin_package(monkeypatch, tmp_path, files: dict[str, str]) -> None:
    """Replace ``app.services.protected_media_plugins`` with a temp on-disk package.

    Writes each ``files`` entry as ``<name>.py`` under a fresh temp directory, then
    points a fake package module's ``__path__`` at it so ``_load_providers()``'s
    ``pkgutil.iter_modules`` + ``importlib.import_module`` walk real files.
    """
    pkg_dir = tmp_path / "fake_protected_media_plugins"
    pkg_dir.mkdir()
    for name, content in files.items():
        (pkg_dir / f"{name}.py").write_text(content)

    fake_pkg = types.ModuleType(PKG_NAME)
    fake_pkg.__path__ = [str(pkg_dir)]
    monkeypatch.setitem(sys.modules, PKG_NAME, fake_pkg)

    # `import app.services.protected_media_plugins as plugin_pkg` binds via
    # ATTRIBUTE traversal from the top-level `app` package once the import
    # machinery has run, not purely via `sys.modules[full_name]` — so the parent
    # package's `protected_media_plugins` attribute (set at process start, when
    # the real plugin package was first imported) must be repointed too, or
    # `_load_providers()` resolves the stale real module instead of this fake one.
    import app.services as _parent_pkg

    monkeypatch.setattr(_parent_pkg, "protected_media_plugins", fake_pkg, raising=False)

    # Drop any previously imported submodules (from a prior test, or the real
    # `mediacms` plugin loaded when this module was first imported) so
    # `pkgutil.iter_modules` re-imports fresh from our temp directory.
    for mod_name in list(sys.modules):
        if mod_name.startswith(PKG_NAME + "."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)


class TestLoadProvidersModuleVariableConvention:
    def test_a_module_level_provider_variable_is_collected(self, monkeypatch, tmp_path):
        _install_fake_plugin_package(
            monkeypatch,
            tmp_path,
            {
                "simple": (
                    "class _P:\n"
                    "    def get_public_auth_config(self):\n"
                    "        return {'hosts': ['x.example.com']}\n"
                    "provider = _P()\n"
                )
            },
        )
        providers = pmp._load_providers()
        assert len(providers) == 1
        assert providers[0].get_public_auth_config() == {"hosts": ["x.example.com"]}

    def test_provider_convention_takes_priority_and_never_calls_get_provider(
        self, monkeypatch, tmp_path
    ):
        # If both are present, `provider` wins and the loader `continue`s before
        # ever touching `get_provider` — so a broken `get_provider` beside a good
        # `provider` must not affect the outcome at all.
        _install_fake_plugin_package(
            monkeypatch,
            tmp_path,
            {
                "both": (
                    "class _P:\n"
                    "    name = 'from-provider-var'\n"
                    "provider = _P()\n"
                    "def get_provider():\n"
                    "    raise RuntimeError('must never be called')\n"
                )
            },
        )
        providers = pmp._load_providers()
        assert len(providers) == 1
        assert getattr(providers[0], "name", None) == "from-provider-var"


class TestLoadProvidersGetProviderConvention:
    def test_a_get_provider_callable_is_invoked_and_collected(self, monkeypatch, tmp_path):
        _install_fake_plugin_package(
            monkeypatch,
            tmp_path,
            {
                "factory": (
                    "class _P:\n    name = 'from-factory'\ndef get_provider():\n    return _P()\n"
                )
            },
        )
        providers = pmp._load_providers()
        assert len(providers) == 1
        assert getattr(providers[0], "name", None) == "from-factory"

    def test_get_provider_raising_is_skipped_without_crashing_the_scan(self, monkeypatch, tmp_path):
        _install_fake_plugin_package(
            monkeypatch,
            tmp_path,
            {
                "broken_factory": ("def get_provider():\n    raise ValueError('cannot build')\n"),
                "good": (
                    "class _P:\n    name = 'still-loaded'\ndef get_provider():\n    return _P()\n"
                ),
            },
        )
        providers = pmp._load_providers()
        names = [getattr(p, "name", None) for p in providers]
        assert names == ["still-loaded"]


class TestLoadProvidersRobustness:
    def test_a_module_that_raises_on_import_is_skipped_not_fatal(self, monkeypatch, tmp_path):
        _install_fake_plugin_package(
            monkeypatch,
            tmp_path,
            {
                "explodes": "raise RuntimeError('boom at import time')\n",
                "good": ("class _P:\n    name = 'survivor'\nprovider = _P()\n"),
            },
        )
        providers = pmp._load_providers()
        names = [getattr(p, "name", None) for p in providers]
        assert names == ["survivor"]

    def test_a_module_with_neither_convention_contributes_nothing(self, monkeypatch, tmp_path):
        _install_fake_plugin_package(
            monkeypatch,
            tmp_path,
            {"irrelevant": "SOME_CONSTANT = 42\n"},
        )
        assert pmp._load_providers() == []

    def test_empty_plugin_package_returns_empty_list(self, monkeypatch, tmp_path):
        _install_fake_plugin_package(monkeypatch, tmp_path, {})
        assert pmp._load_providers() == []

    def test_package_missing_a_path_attribute_returns_empty_list(self, monkeypatch, tmp_path):
        # A plain (non-package) module has no `__path__` at all — the loader must
        # detect this and bail out rather than raising on `pkgutil.iter_modules(None, ...)`.
        import app.services as _parent_pkg

        fake_mod = types.ModuleType(PKG_NAME)
        monkeypatch.setitem(sys.modules, PKG_NAME, fake_mod)
        monkeypatch.setattr(_parent_pkg, "protected_media_plugins", fake_mod, raising=False)
        assert pmp._load_providers() == []

    def test_package_that_cannot_be_imported_at_all_returns_empty_list(self, monkeypatch):
        # `sys.modules[name] = None` is the documented CPython mechanism to force
        # `import x` to raise ImportError ("import of x halted; None in sys.modules")
        # without needing to actually delete the real package from disk.
        monkeypatch.setitem(sys.modules, PKG_NAME, None)
        assert pmp._load_providers() == []


class _FakeProvider:
    """Plain duck-typed stand-in for a ProtectedMediaProvider — no unittest.mock."""

    def __init__(self, config=None, *, raises: bool = False):
        self._config = config or {}
        self._raises = raises

    def get_public_auth_config(self):
        if self._raises:
            raise RuntimeError("provider is misconfigured")
        return self._config


class _NoConfigMethodProvider:
    """A provider object that simply has no ``get_public_auth_config`` at all."""

    name = "no-config-method"


class TestGetProtectedMediaAuthConfig:
    def test_aggregates_configs_from_multiple_providers_in_order(self, monkeypatch):
        p1 = _FakeProvider({"hosts": ["a.example.com"], "auth_type": "user_password"})
        p2 = _FakeProvider({"hosts": ["b.example.com"], "auth_type": "api_key"})
        monkeypatch.setattr(pmp, "PROTECTED_MEDIA_PROVIDERS", [p1, p2])

        configs = pmp.get_protected_media_auth_config()

        assert configs == [
            {"hosts": ["a.example.com"], "auth_type": "user_password"},
            {"hosts": ["b.example.com"], "auth_type": "api_key"},
        ]

    def test_a_provider_returning_an_empty_dict_is_excluded(self, monkeypatch):
        p1 = _FakeProvider({"hosts": ["a.example.com"]})
        p2 = _FakeProvider({})  # opted out of public config
        monkeypatch.setattr(pmp, "PROTECTED_MEDIA_PROVIDERS", [p1, p2])

        configs = pmp.get_protected_media_auth_config()
        assert configs == [{"hosts": ["a.example.com"]}]

    def test_a_provider_with_no_get_public_auth_config_method_is_skipped(self, monkeypatch):
        p1 = _FakeProvider({"hosts": ["a.example.com"]})
        p2 = _NoConfigMethodProvider()
        monkeypatch.setattr(pmp, "PROTECTED_MEDIA_PROVIDERS", [p1, p2])

        configs = pmp.get_protected_media_auth_config()
        assert configs == [{"hosts": ["a.example.com"]}]

    def test_a_provider_whose_get_public_auth_config_raises_is_skipped_not_fatal(self, monkeypatch):
        p1 = _FakeProvider({"hosts": ["a.example.com"]})
        p2 = _FakeProvider(raises=True)
        p3 = _FakeProvider({"hosts": ["c.example.com"]})
        monkeypatch.setattr(pmp, "PROTECTED_MEDIA_PROVIDERS", [p1, p2, p3])

        configs = pmp.get_protected_media_auth_config()
        assert configs == [
            {"hosts": ["a.example.com"]},
            {"hosts": ["c.example.com"]},
        ]

    def test_empty_registry_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(pmp, "PROTECTED_MEDIA_PROVIDERS", [])
        assert pmp.get_protected_media_auth_config() == []

    def test_raising_provider_is_logged(self, monkeypatch, caplog):
        import logging

        p1 = _FakeProvider(raises=True)
        monkeypatch.setattr(pmp, "PROTECTED_MEDIA_PROVIDERS", [p1])

        with caplog.at_level(logging.WARNING, logger="app.services.protected_media_providers"):
            configs = pmp.get_protected_media_auth_config()

        assert configs == []
        assert any("get_public_auth_config" in r.message for r in caplog.records)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
