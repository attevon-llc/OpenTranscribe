"""Real behavioral tests for the SCIM PATCH parser (RFC 7644 §3.5.2).

``app/api/endpoints/scim/patch_ops.py`` is a pure function module — no DB, no
network — that decides, for every PATCH an IdP connector can send, whether it is one
of the supported shapes or a refused ``400 invalidPath``. It has zero test coverage
(issue #474). Its own module docstring is explicit that the whole point is to refuse
rather than silently no-op an unsupported shape, so these tests assert on the parsed
*values*, not just that no exception was raised.
"""

from __future__ import annotations

import pytest

from app.api.endpoints.scim.errors import SCIM_TYPE_INVALID_PATH
from app.api.endpoints.scim.errors import SCIM_TYPE_INVALID_SYNTAX
from app.api.endpoints.scim.errors import SCIMError
from app.api.endpoints.scim.patch_ops import build_user_update
from app.api.endpoints.scim.patch_ops import parse_group_operation
from app.schemas.scim import SCIMPatchOperation


def _op(op: str, path: str | None = None, value=None) -> SCIMPatchOperation:
    return SCIMPatchOperation(op=op, path=path, value=value)


class TestBuildUserUpdateEntraShape:
    """``op: replace`` with no path and an object value."""

    def test_pathless_replace_maps_recognised_top_level_keys(self):
        updates = build_user_update(
            [
                _op(
                    "replace",
                    value={
                        "active": True,
                        "userName": "alice@example.com",
                        "externalId": "ext-1",
                        "displayName": "Alice Example",
                    },
                )
            ]
        )
        assert updates == {
            "active": True,
            "email": "alice@example.com",
            "external_id": "ext-1",
            "display_name": "Alice Example",
        }

    def test_pathless_replace_expands_nested_name_object(self):
        updates = build_user_update(
            [_op("replace", value={"name": {"givenName": "Alice", "familyName": "Example"}})]
        )
        assert updates == {"given_name": "Alice", "family_name": "Example"}

    def test_pathless_replace_coerces_string_boolean_active(self):
        # Okta/Entra connectors send "true"/"false" strings for booleans in some flows.
        updates = build_user_update([_op("replace", value={"active": "false"})])
        assert updates == {"active": False}

    def test_pathless_replace_rejects_non_object_value(self):
        with pytest.raises(SCIMError) as excinfo:
            build_user_update([_op("replace", value="not-an-object")])
        assert excinfo.value.status_code == 400
        assert excinfo.value.scim_type == SCIM_TYPE_INVALID_SYNTAX

    def test_pathless_replace_rejects_unrecognised_top_level_key(self):
        with pytest.raises(SCIMError) as excinfo:
            build_user_update([_op("replace", value={"nickName": "Al"})])
        assert excinfo.value.status_code == 400
        assert excinfo.value.scim_type == SCIM_TYPE_INVALID_PATH

    def test_pathless_replace_rejects_unrecognised_name_subattribute(self):
        with pytest.raises(SCIMError) as excinfo:
            build_user_update([_op("replace", value={"name": {"formatted": "Alice Example"}})])
        assert excinfo.value.status_code == 400
        assert excinfo.value.scim_type == SCIM_TYPE_INVALID_PATH

    def test_pathless_add_uses_the_same_object_shape_as_replace(self):
        # `op not in (add, replace)` is rejected, and neither op requires a path, so
        # a pathless "add" must fold the same way a pathless "replace" does.
        updates = build_user_update([_op("add", value={"active": True})])
        assert updates == {"active": True}

    def test_active_non_boolean_string_is_rejected(self):
        with pytest.raises(SCIMError) as excinfo:
            build_user_update([_op("replace", value={"active": "maybe"})])
        assert excinfo.value.status_code == 400


class TestBuildUserUpdateOktaShape:
    """``op: replace``/``add`` with a single attribute ``path``."""

    @pytest.mark.parametrize("op", ["add", "replace"])
    @pytest.mark.parametrize(
        "path,value,expected_key,expected_value",
        [
            ("active", True, "active", True),
            ("userName", "bob@example.com", "email", "bob@example.com"),
            ("externalId", "ext-42", "external_id", "ext-42"),
            ("displayName", "Bob Example", "display_name", "Bob Example"),
            ("name.givenName", "Bob", "given_name", "Bob"),
            ("name.familyName", "Example", "family_name", "Example"),
        ],
    )
    def test_single_attribute_path_maps_to_update_kwarg(
        self, op, path, value, expected_key, expected_value
    ):
        updates = build_user_update([_op(op, path=path, value=value)])
        assert updates == {expected_key: expected_value}

    def test_path_matching_is_case_insensitive(self):
        updates = build_user_update([_op("replace", path="USERNAME", value="carol@example.com")])
        assert updates == {"email": "carol@example.com"}

    def test_active_path_coerces_string_boolean(self):
        updates = build_user_update([_op("add", path="active", value="TRUE")])
        assert updates == {"active": True}

    def test_active_path_rejects_non_boolean(self):
        with pytest.raises(SCIMError) as excinfo:
            build_user_update([_op("replace", path="active", value="yes")])
        assert excinfo.value.status_code == 400

    def test_unsupported_path_is_refused_not_ignored(self):
        with pytest.raises(SCIMError) as excinfo:
            build_user_update([_op("replace", path='emails[type eq "work"].value', value="x")])
        assert excinfo.value.status_code == 400
        assert excinfo.value.scim_type == SCIM_TYPE_INVALID_PATH

    def test_multiple_operations_accumulate_into_one_kwargs_dict(self):
        updates = build_user_update(
            [
                _op("replace", path="active", value=True),
                _op("add", path="displayName", value="Dana Example"),
            ]
        )
        assert updates == {"active": True, "display_name": "Dana Example"}

    def test_later_operation_overwrites_an_earlier_one_on_the_same_key(self):
        updates = build_user_update(
            [
                _op("replace", path="displayName", value="Old Name"),
                _op("replace", path="displayName", value="New Name"),
            ]
        )
        assert updates == {"display_name": "New Name"}


class TestBuildUserUpdateRefusals:
    def test_remove_op_on_a_user_is_refused(self):
        with pytest.raises(SCIMError) as excinfo:
            build_user_update([_op("remove", path="displayName")])
        assert excinfo.value.status_code == 400
        assert excinfo.value.scim_type == SCIM_TYPE_INVALID_PATH

    def test_remove_op_is_refused_even_without_a_path(self):
        with pytest.raises(SCIMError):
            build_user_update([_op("remove", value={"active": False})])

    def test_unsupported_op_is_refused(self):
        with pytest.raises(SCIMError) as excinfo:
            build_user_update([_op("invalidOp", path="active", value=True)])
        assert excinfo.value.status_code == 400
        assert excinfo.value.scim_type == SCIM_TYPE_INVALID_SYNTAX

    def test_op_matching_is_case_insensitive(self):
        updates = build_user_update([_op("REPLACE", path="active", value=True)])
        assert updates == {"active": True}

    def test_empty_operations_list_returns_empty_updates(self):
        assert build_user_update([]) == {}


class TestParseGroupOperationMembers:
    def test_add_members_returns_the_id_set(self):
        kind, value = parse_group_operation(
            _op("add", path="members", value=[{"value": "u1"}, {"value": "u2"}])
        )
        assert kind == "members_add"
        assert value == {"u1", "u2"}

    def test_add_members_accepts_a_single_object_value(self):
        kind, value = parse_group_operation(_op("add", path="members", value={"value": "u1"}))
        assert kind == "members_add"
        assert value == {"u1"}

    def test_add_members_rejects_non_list_non_dict_value(self):
        with pytest.raises(SCIMError):
            parse_group_operation(_op("add", path="members", value="u1"))

    def test_add_members_rejects_an_entry_missing_value(self):
        with pytest.raises(SCIMError):
            parse_group_operation(_op("add", path="members", value=[{"notvalue": "u1"}]))

    def test_remove_members_with_explicit_ids_removes_only_those_ids(self):
        kind, value = parse_group_operation(_op("remove", path="members", value=[{"value": "u1"}]))
        assert kind == "members_remove"
        assert value == {"u1"}

    def test_bare_remove_members_with_no_value_empties_the_group(self):
        """A bare ``remove`` on ``members`` (no value) must actually clear membership.

        This is the shape Okta/Entra send to empty a group. The module docstring
        promises refusal over a silent no-op for anything unsupported; a bare
        ``remove`` IS supported (the docstring/inline comment both say so), so it
        must not silently do nothing either. ``members_replace`` with an empty set
        is what reaches ``set_group_members`` and actually deletes every
        SCIM-owned row (see ``scim_group_service.remove_group_members``, which
        no-ops on an empty id set — ``members_remove`` with an empty set would be
        exactly that no-op).
        """
        kind, value = parse_group_operation(_op("remove", path="members", value=None))
        assert kind == "members_replace"
        assert value == set()

    def test_bare_remove_members_with_empty_list_also_empties_the_group(self):
        kind, value = parse_group_operation(_op("remove", path="members", value=[]))
        assert kind == "members_replace"
        assert value == set()

    def test_replace_members_returns_the_full_replacement_set(self):
        kind, value = parse_group_operation(
            _op("replace", path="members", value=[{"value": "u1"}, {"value": "u2"}])
        )
        assert kind == "members_replace"
        assert value == {"u1", "u2"}

    def test_members_path_is_case_insensitive(self):
        kind, value = parse_group_operation(_op("add", path="MEMBERS", value=[{"value": "u1"}]))
        assert kind == "members_add"
        assert value == {"u1"}

    def test_unsupported_op_on_members_path_is_refused(self):
        with pytest.raises(SCIMError):
            parse_group_operation(_op("invalidOp", path="members", value=[{"value": "u1"}]))


class TestParseGroupOperationValuePath:
    """``members[value eq "<id>"]`` — the Entra-specific value-path form."""

    @pytest.mark.parametrize(
        "path",
        [
            'members[value eq "u1"]',
            "members[value eq 'u1']",
            '  members [ value  eq  "u1" ]  ',
            'MEMBERS[VALUE EQ "u1"]',
        ],
    )
    def test_value_path_remove_extracts_the_single_id(self, path):
        kind, value = parse_group_operation(_op("remove", path=path))
        assert kind == "members_remove"
        assert value == {"u1"}

    def test_value_path_is_refused_for_any_op_other_than_remove(self):
        with pytest.raises(SCIMError) as excinfo:
            parse_group_operation(_op("add", path='members[value eq "u1"]', value=None))
        assert excinfo.value.status_code == 400
        assert excinfo.value.scim_type == SCIM_TYPE_INVALID_PATH

    def test_value_path_with_unsupported_operator_falls_through_to_refusal(self):
        # `ne` is not `eq`, so the regex does not match this at all — it must fall
        # through to the generic "unsupported path" refusal, not be silently accepted.
        with pytest.raises(SCIMError):
            parse_group_operation(_op("remove", path='members[value ne "u1"]'))


class TestParseGroupOperationDisplayName:
    @pytest.mark.parametrize("op", ["add", "replace"])
    def test_display_name_replace_returns_the_stripped_string(self, op):
        kind, value = parse_group_operation(_op(op, path="displayName", value="  New Name  "))
        assert kind == "display_name"
        assert value == "New Name"

    def test_display_name_remove_is_refused(self):
        with pytest.raises(SCIMError) as excinfo:
            parse_group_operation(_op("remove", path="displayName", value="x"))
        assert excinfo.value.status_code == 400
        assert excinfo.value.scim_type == SCIM_TYPE_INVALID_PATH

    def test_display_name_rejects_empty_string(self):
        with pytest.raises(SCIMError):
            parse_group_operation(_op("replace", path="displayName", value="   "))

    def test_display_name_rejects_non_string_value(self):
        with pytest.raises(SCIMError):
            parse_group_operation(_op("replace", path="displayName", value=123))


class TestParseGroupOperationRefusals:
    def test_unrecognised_path_is_refused_with_invalid_path(self):
        with pytest.raises(SCIMError) as excinfo:
            parse_group_operation(_op("replace", path="externalId", value="ext-1"))
        assert excinfo.value.status_code == 400
        assert excinfo.value.scim_type == SCIM_TYPE_INVALID_PATH

    def test_no_path_and_no_recognised_shape_is_refused(self):
        with pytest.raises(SCIMError):
            parse_group_operation(_op("replace", path=None, value="x"))
