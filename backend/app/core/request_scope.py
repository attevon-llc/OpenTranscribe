"""The slice of an inbound request that non-HTTP layers may read.

Capability resolvers, the content-audit writer, the client-IP resolver and external token
verifiers all want a few facts about the caller (peer address, a header, the per-request
state bag) but must not import a web framework to say so. ``RequestScope`` is the
structural type for exactly that: a Starlette/FastAPI ``Request`` or ``WebSocket``
satisfies it without adaptation, so routers keep passing the request they already hold
(Starlette docs, "Requests": ``client``, ``headers``, ``state``).
"""

from __future__ import annotations

from typing import Any
from typing import Protocol


class Peer(Protocol):
    """The remote socket address (Starlette's ``Address``)."""

    @property
    def host(self) -> str: ...


class HeaderReader(Protocol):
    """Case-insensitive header lookup (Starlette's ``Headers``)."""

    def get(self, key: str, /) -> str | None: ...


class StateBag(Protocol):
    """The per-request attribute bag (Starlette's ``State``): dynamic by design.

    Middleware and dependencies stash values on it (``external_identity``, ``org_id``,
    ``client_ip``); the attribute names are a contract between them, not a schema.
    """

    def __getattr__(self, name: str) -> Any: ...

    def __setattr__(self, name: str, value: Any) -> None: ...


class RequestScope(Protocol):
    """What a non-HTTP layer is allowed to know about the request it serves."""

    @property
    def client(self) -> Peer | None: ...

    @property
    def headers(self) -> HeaderReader: ...

    @property
    def state(self) -> StateBag: ...
