"""A scripted directory behind the ldap3 boundary, for the LDAP auth and probe tests.

:func:`install` replaces ``Server`` and ``Connection`` in ``app.auth.ldap_directory``, the module
whose ``_get_ldap_server`` / ``_connect_and_bind`` construct them, so login
(``ldap_authenticate``), the probes (``ldap_directory_session`` / ``probe_ldap_user``) and the
helpers all run their real code against one :class:`FakeDirectory`.

Searches are answered from an exact ``(search_base, search_filter)`` table, so a test that
expects a result also pins the exact (escaped) filter text the code produced. Every call that
crosses the boundary is appended to ``FakeDirectory.calls`` in order.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from ldap3.core.exceptions import LDAPException

SERVICE_DN = "cn=svc,dc=test"
SERVICE_CREDENTIAL = "svc-credential"
SEARCH_BASE = "dc=test"


class FakeAttr:
    def __init__(self, value: Any) -> None:
        self.value = value


class FakeEntry:
    """Minimal ldap3 ``Entry``: ``in``, ``entry[attr].value``, ``getattr`` and ``entry_dn``."""

    def __init__(self, dn: str, attrs: dict[str, Any]) -> None:
        self.entry_dn = dn
        self._attrs = dict(attrs)

    def __contains__(self, key: str) -> bool:
        return key in self._attrs

    def __getitem__(self, key: str) -> FakeAttr:
        return FakeAttr(self._attrs[key])

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        if key in self._attrs:
            return self._attrs[key]
        raise AttributeError(key)


@dataclass
class FakeDirectory:
    """What the directory holds and how it misbehaves; ``calls`` records what was asked."""

    searches: dict[tuple[str, str], Any] = field(default_factory=dict)
    passwords: dict[str, str] = field(default_factory=lambda: {SERVICE_DN: SERVICE_CREDENTIAL})
    bind_errors: dict[str, Exception] = field(default_factory=dict)
    start_tls_result: bool = True
    server_error: Exception | None = None
    rejected_attribute: str | None = None
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def add_user(self, entry: FakeEntry, password: str, filter_text: str | None = None) -> None:
        """Make ``entry`` the single answer to ``filter_text`` (default ``(uid=<uid>)``)."""
        key = filter_text or f"(uid={entry._attrs['uid']})"
        self.searches[(SEARCH_BASE, key)] = [entry]
        self.passwords[entry.entry_dn] = password

    def binds(self) -> list[tuple[str, str]]:
        return [(c[1], c[2]) for c in self.calls if c[0] == "bind"]

    def searches_made(self) -> list[dict[str, Any]]:
        return [c[1] for c in self.calls if c[0] == "search"]

    def unbinds(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "unbind"]


class _Server:
    def __init__(self, directory: FakeDirectory, host: str, **kwargs: Any) -> None:
        directory.calls.append(("server", host, kwargs))
        if directory.server_error is not None:
            raise directory.server_error
        self.host = host


class _Connection:
    def __init__(
        self, directory: FakeDirectory, server: Any, user: str, password: str, auto_bind: bool
    ) -> None:
        self._dir = directory
        self.user = user
        self.password = password
        self.bound = False
        self.entries: list[Any] = []
        directory.calls.append(("connection", user, auto_bind))

    def open(self) -> bool:
        self._dir.calls.append(("open", self.user))
        return True

    def start_tls(self) -> bool:
        self._dir.calls.append(("start_tls", self.user))
        return self._dir.start_tls_result

    def bind(self) -> bool:
        self._dir.calls.append(("bind", self.user, self.password))
        error = self._dir.bind_errors.get(self.user)
        if error is not None:
            raise error
        self.bound = self._dir.passwords.get(self.user) == self.password
        return self.bound

    def search(self, search_base: str, search_filter: str, **kwargs: Any) -> bool:
        attributes = kwargs.get("attributes")
        self._dir.calls.append(("search", {"base": search_base, "filter": search_filter, **kwargs}))
        rejected = self._dir.rejected_attribute
        if rejected and attributes and rejected in attributes:
            raise LDAPException(f"invalid attribute type {rejected}")
        answer = self._dir.searches.get((search_base, search_filter), [])
        if isinstance(answer, Exception):
            raise answer
        self.entries = list(answer)
        return bool(self.entries)

    def unbind(self) -> None:
        self._dir.calls.append(("unbind", self.user))
        self.bound = False


def install(monkeypatch: Any, directory: FakeDirectory | None = None) -> FakeDirectory:
    """Route every LDAP connection through ``directory`` (a fresh one when omitted)."""
    directory = directory or FakeDirectory()
    monkeypatch.setattr(
        "app.auth.ldap_directory.Server", lambda host, **kw: _Server(directory, host, **kw)
    )
    monkeypatch.setattr(
        "app.auth.ldap_directory.Connection",
        lambda server, user, password, auto_bind: _Connection(
            directory, server, user, password, auto_bind
        ),
    )
    return directory


def bound_service_connection(directory: FakeDirectory) -> _Connection:
    """A service-account connection that is already bound, for calling helpers directly."""
    conn = _Connection(directory, None, SERVICE_DN, SERVICE_CREDENTIAL, False)
    conn.bound = True
    directory.calls.clear()
    return conn
