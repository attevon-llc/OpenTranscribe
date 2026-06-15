"""Tenant-scope primitives shared across SQL + search planes (cloud-edition seam).

Kept dependency-free (stdlib only) so any layer — utils, services, endpoints —
can import the ``UNSCOPED`` sentinel and ``OrgScope`` type without pulling in the
FastAPI/auth chain. ``app.api.deps_context`` re-exports these for the plan's
"reuse ``scope_to_context``" intent.
"""


class _Unscoped:
    """Sentinel: 'no tenant context was threaded to this call' (legacy path).

    Distinguishes an un-threaded legacy caller (apply NO org gate — preserve
    pre-cloud behavior) from an explicitly-personal request (org id is ``None``
    — apply the default-deny gate so org rows can't leak into personal scope).
    Community-edition callers never pass an org id, so they get the sentinel and
    behave exactly as before.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "UNSCOPED"


UNSCOPED = _Unscoped()

# A tenant-scope argument is either an explicit org id, explicit personal (None),
# or the UNSCOPED sentinel meaning "no context threaded — don't gate".
OrgScope = int | None | _Unscoped
