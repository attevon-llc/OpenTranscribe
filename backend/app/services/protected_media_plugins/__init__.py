"""Protected-media provider plugins, discovered by ``app.services.protected_media_providers``.

Every module placed in this package is imported at startup. A module registers a provider
in one of two ways: it binds a module-level ``provider`` object, or it defines a
``get_provider()`` function that builds and returns one. When both are present the
``provider`` object is used and ``get_provider()`` is never called. A module offering
neither is ignored.
"""
