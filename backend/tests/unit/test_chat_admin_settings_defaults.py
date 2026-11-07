"""``ChatAdminSettings`` must not carry its own copy of the chat defaults.

The admin GET builds it from ``get_chat_settings(db).as_dict()``, so a literal default on the
schema is never what the server applies; it only shows up in the OpenAPI document and in any
code that builds the schema without arguments, and it drifts (``max_concurrent_streams`` read 2
there while the server applied 6).
"""

from __future__ import annotations

from app.schemas.chat import ChatAdminSettings
from app.services.chat.settings import ChatSettings


def test_every_schema_default_equals_the_runtime_default() -> None:
    runtime = ChatSettings().as_dict()
    schema = ChatAdminSettings().model_dump()
    shared = sorted(set(schema) & set(runtime))
    assert shared, "the two models no longer share any field: the comparison would be vacuous"
    drift = {k: (schema[k], runtime[k]) for k in shared if schema[k] != runtime[k]}
    assert drift == {}, f"schema default != runtime default (schema, runtime): {drift}"
