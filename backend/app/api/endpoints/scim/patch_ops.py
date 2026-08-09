"""The exact ``PATCH`` surface this server implements (RFC 7644 §3.5.2).

``PATCH`` is where a SCIM implementation becomes large, and where Okta and Entra
exercise genuinely different subsets. Rather than half-implement the path grammar
and quietly ignore what does not parse — which returns 200 for a change that did not
happen — this module implements a closed set and **refuses everything else** with
``400 invalidPath``.

Supported
---------
Users:
  * ``op: replace`` with **no path** and an object value, whose recognised keys are
    ``active``, ``userName``, ``externalId``, ``displayName``,
    ``name.givenName``/``name.familyName`` (dotted, as Entra sends them) and a
    nested ``name`` object. This is the shape Entra ID uses for everything.
  * ``op: replace`` / ``op: add`` with ``path`` ∈ ``active``, ``userName``,
    ``externalId``, ``displayName``, ``name.givenName``, ``name.familyName``. This
    is the shape Okta uses. ``add`` and ``replace`` are equivalent for these
    single-valued attributes, which RFC 7644 §3.5.2.1 permits.

Groups:
  * ``op: add`` with ``path: "members"`` and a list of ``{"value": <id>}``.
  * ``op: remove`` with ``path: "members"`` and such a list — **and** the value-path
    form ``members[value eq "<id>"]``, because Entra sends that one and it is not
    optional in practice.
  * ``op: replace`` with ``path: "members"`` (full replacement of SCIM-owned rows).
  * ``op: replace`` with ``path: "displayName"``.

NOT supported, and refused rather than ignored
----------------------------------------------
  * ``op: remove`` on a User attribute. There is no attribute here whose removal is
    meaningful — clearing ``userName`` would orphan the account — so it is an error,
    not a no-op.
  * Any value-path filter other than ``members[value eq "..."]``: no ``and``/``or``,
    no operator but ``eq``, no attribute but ``value``.
  * Sub-attribute paths beyond the two ``name.*`` entries above (``emails[type eq
    "work"].value``, ``addresses``, ``phoneNumbers``, ``photos``, ``entitlements``,
    ``roles``, ``x509Certificates``) — none of them map to anything this
    application stores.
  * Enterprise User extension attributes (``manager``, ``department``, ``costCenter``).
  * ``op: remove`` on a Group's ``displayName``.

The refusals are listed in ``ServiceProviderConfig`` prose and repeated in the error
detail, so an administrator reading their IdP's connector log learns which
attribute mapping to delete.
"""

from __future__ import annotations

import re
from typing import Any

from app.api.endpoints.scim.errors import SCIM_TYPE_INVALID_PATH
from app.api.endpoints.scim.errors import SCIM_TYPE_INVALID_SYNTAX
from app.api.endpoints.scim.errors import SCIMError
from app.api.endpoints.scim.errors import bad_request
from app.schemas.scim import SCIMPatchOperation

#: User attributes a PATCH may set, in SCIM spelling -> our update_user kwarg.
USER_PATCHABLE = {
    "active": "active",
    "username": "email",
    "externalid": "external_id",
    "displayname": "display_name",
    "name.givenname": "given_name",
    "name.familyname": "family_name",
}

#: ``members[value eq "<id>"]`` — the one value-path form that is supported.
_MEMBER_VALUE_PATH = re.compile(
    r'^\s*members\s*\[\s*value\s+eq\s+["\'](?P<value>[^"\']+)["\']\s*\]\s*$',
    re.IGNORECASE,
)


def _as_bool(value: Any, attribute: str) -> bool:
    """Coerce a SCIM boolean, accepting the string forms connectors send."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise bad_request(f"{attribute} must be a boolean, got {value!r}")


def _assign(target: dict[str, Any], attribute: str, value: Any) -> None:
    """Record one recognised User attribute into the update kwargs."""
    key = USER_PATCHABLE.get(attribute.lower())
    if key is None:
        raise SCIMError(
            400,
            (
                f"Unsupported PATCH path {attribute!r}. Supported User attributes: "
                f"{', '.join(sorted(USER_PATCHABLE))}."
            ),
            scim_type=SCIM_TYPE_INVALID_PATH,
        )
    target[key] = _as_bool(value, attribute) if key == "active" else value


def _apply_valueless_replace(target: dict[str, Any], value: Any) -> None:
    """Handle ``{"op": "replace", "value": {...}}`` — the Entra shape."""
    if not isinstance(value, dict):
        raise bad_request("A pathless replace must carry an object value", SCIM_TYPE_INVALID_SYNTAX)
    for raw_key, raw_value in value.items():
        if raw_key.lower() == "name" and isinstance(raw_value, dict):
            for part, part_value in raw_value.items():
                _assign(target, f"name.{part}", part_value)
            continue
        _assign(target, raw_key, raw_value)


def build_user_update(operations: list[SCIMPatchOperation]) -> dict[str, Any]:
    """Fold a User ``PatchOp`` into keyword arguments for ``scim_service.update_user``.

    Args:
        operations: The ``Operations`` array, in order.

    Returns:
        Kwargs for ``update_user``. ``given_name``/``family_name`` are collapsed into
        a single ``display_name`` by the caller, which is the only name field the
        ``user`` table has.

    Raises:
        SCIMError: 400 for an unsupported op, path, or value shape.
    """
    updates: dict[str, Any] = {}
    for operation in operations:
        op = (operation.op or "").strip().lower()
        if op == "remove":
            raise SCIMError(
                400,
                "PATCH 'remove' is not supported on a User: no attribute this server "
                "stores can meaningfully be removed. Use 'replace' with active=false "
                "to deactivate.",
                scim_type=SCIM_TYPE_INVALID_PATH,
            )
        if op not in ("add", "replace"):
            raise bad_request(f"Unsupported PATCH op {operation.op!r}", SCIM_TYPE_INVALID_SYNTAX)
        if not operation.path:
            _apply_valueless_replace(updates, operation.value)
            continue
        _assign(updates, operation.path, operation.value)
    return updates


def _member_ids(value: Any) -> set[str]:
    """Extract member ids from a ``members`` operation value."""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise bad_request("A members operation must carry a list of member objects")
    ids: set[str] = set()
    for entry in value:
        if isinstance(entry, dict) and entry.get("value"):
            ids.add(str(entry["value"]))
        elif isinstance(entry, str):
            ids.add(entry)
        else:
            raise bad_request(f"Unrecognised member entry {entry!r}")
    return ids


def parse_group_operation(operation: SCIMPatchOperation) -> tuple[str, Any]:
    """Classify one Group PATCH operation.

    Returns:
        ``("members_add" | "members_remove" | "members_replace", set[str])`` or
        ``("display_name", str)``.

    Raises:
        SCIMError: 400 for anything outside the supported set (see module docstring).
    """
    op = (operation.op or "").strip().lower()
    path = (operation.path or "").strip()

    value_path = _MEMBER_VALUE_PATH.match(path) if path else None
    if value_path:
        if op != "remove":
            raise SCIMError(
                400,
                "The members[value eq \"...\"] path form is supported only with op 'remove'.",
                scim_type=SCIM_TYPE_INVALID_PATH,
            )
        return "members_remove", {value_path.group("value")}

    if path.lower() == "members":
        if op == "add":
            return "members_add", _member_ids(operation.value)
        if op == "remove":
            # A bare `remove` on `members` with no value means "empty the group".
            return "members_remove", _member_ids(operation.value) if operation.value else set()
        if op == "replace":
            return "members_replace", _member_ids(operation.value)
        raise bad_request(f"Unsupported PATCH op {operation.op!r} on members")

    if path.lower() == "displayname" and op in ("add", "replace"):
        if not isinstance(operation.value, str) or not operation.value.strip():
            raise bad_request("displayName must be a non-empty string")
        return "display_name", operation.value.strip()

    raise SCIMError(
        400,
        (
            f"Unsupported PATCH path {operation.path!r} on a Group. Supported: "
            "'members' (add/remove/replace), 'members[value eq \"<id>\"]' (remove), "
            "and 'displayName' (replace)."
        ),
        scim_type=SCIM_TYPE_INVALID_PATH,
    )
