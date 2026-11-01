"""Schema detection, v377-v388: the auth-identity chain and the v384-v388 clean-ups.

Invitations, IdP group mapping, the OIDC rename (data-only v379), approval state, SCIM, SAML,
the chat reasoning column, the orphan-table drop, tag sharing, the actor foreign keys and the
group tenancy stamp. Every arm requires the full v376 fingerprint from ``cloud_seams``.

``probe()`` issues this era's queries in the order the ladder always has; ``ARMS`` is this
era's slice of the newest-first ladder (``ladder.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from app.db.schema_detect.cloud_seams import matches_v376
from app.db.schema_detect.probe import check_exists

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from app.db.schema_detect.ladder import Arm


@dataclass(frozen=True)
class AuthIdentityFacts:
    """Probe answers for v377-v388."""

    has_auth_type_check: bool
    has_user_invitation: bool
    has_group_mapping: bool
    has_membership_source: bool
    has_oidc_subject: bool
    has_oidc_user_refresh_token: bool
    has_session_id_token: bool
    has_user_approval_status: bool
    has_approval_status_check: bool
    has_scim_token: bool
    has_proxy_group_source: bool
    has_saml_subject: bool
    has_saml_auth_type_check: bool
    has_chat_reasoning_content: bool
    has_orphan_tables: bool
    has_legacy_oidc_config_keys: bool
    has_tag_share: bool
    has_actor_fk_set_null: bool
    has_tag_share_type_check: bool
    has_legacy_role_check: bool
    has_user_group_org: bool


def probe(conn: Connection, tables: list[str], has_auth_config: bool) -> AuthIdentityFacts:
    """Run this era's probes against *conn*, in ladder order."""
    _check_exists = partial(check_exists, conn)
    return AuthIdentityFacts(
        # v377 guard (formerly v375 on this branch, renumbered — see NOTE ON THE
        # RENUMBERING below): the auth_type CHECK plus the invitation table. The
        # constraint alone was the marker while the revision only hardened the user
        # columns; it now also creates user_invitation / email_verification_token and
        # the user.email_verified columns, so a DB that has the constraint but not
        # the tables predates the extension and must NOT be stamped v377 — it would
        # never receive the new DDL.
        has_auth_type_check=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_auth_type_valid')"
        ),
        has_user_invitation="user_invitation" in tables,
        # v378 guards (formerly v376): IdP group mapping. BOTH markers are required —
        # the mapping table is useless without user_group_member.source (reconciliation
        # could not tell a directory-derived membership from a hand-added one and would
        # either never revoke or wipe manual work), so a DB carrying only one of them
        # predates the revision and must still receive its DDL.
        has_group_mapping="group_mapping" in tables,
        has_membership_source=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user_group_member' AND column_name = 'source')"
        ),
        # v380 guards (formerly v378): the OIDC identity rename. THREE markers, all
        # required — the revision is a single transaction, so a schema carrying only
        # part of it is a hand-edited database and must stamp lower and receive the
        # whole thing.
        has_oidc_subject=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'oidc_subject')"
        ),
        has_oidc_user_refresh_token=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'oidc_refresh_token')"
        ),
        has_session_id_token=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'refresh_token' AND column_name = 'oidc_id_token')"
        ),
        # v381 guard (formerly v379): administrator approval of newly provisioned
        # accounts. TWO markers. The column alone is not the revision: without
        # ck_user_approval_status_valid an unrecognised value reads as neither pending
        # nor rejected, so the gate in api/endpoints/auth/lifecycle_gates.py fails OPEN —
        # which is precisely the state a database must not be stamped as already having.
        has_user_approval_status=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'approval_status')"
        ),
        has_approval_status_check=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_approval_status_valid')"
        ),
        # v382 guards (formerly v380): SCIM tokens and the widened group-source CHECKs.
        # TWO markers, because the revision does two things and a schema carrying only
        # one of them is hand-edited: with the table but not the widened CHECK, every
        # proxy login and every SCIM group write would fail on a CheckViolation, which
        # is the sort of thing a re-run must be able to repair.
        has_scim_token="scim_token" in tables,
        has_proxy_group_source=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_group_mapping_source_valid' "
            "AND pg_get_constraintdef(oid) LIKE '%proxy%')"
        ),
        # v383 guards (formerly v381): the SAML auth-type CHECK widening and its
        # identity column. Same two-marker reasoning as v382 — a schema carrying the
        # column without the widened CHECK would refuse every SAML JIT provision on a
        # CheckViolation.
        has_saml_subject=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'saml_subject')"
        ),
        has_saml_auth_type_check=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_user_auth_type_valid' "
            "AND pg_get_constraintdef(oid) LIKE '%saml%')"
        ),
        # v384 guard: the collapsible reasoning-display column on chat_message. A
        # single nullable TEXT column, so one marker is the whole revision.
        has_chat_reasoning_content=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'chat_message' AND column_name = 'reasoning_content')"
        ),
        # v385 guard: three orphan tables dropped (issue #398). This revision REMOVES
        # objects, so the fingerprint is an absence, not a presence — the probe is
        # inverted relative to every additive revision above. All three must be gone;
        # a database with any of them still present predates v385.
        has_orphan_tables=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name IN ('upload_session', 'speaker_audio_clip', "
            "'user_certificate_preferences'))"
        ),
        # v379 guard (formerly v377): the auth-config data rename. This revision adds
        # NO DDL, so there is no column to probe — the fingerprint is the absence of
        # the retired key prefix in both config tables. A deployment that never
        # configured OIDC has no matching rows either way, which is correct: v379 is
        # a no-op there, so stamping it costs nothing and re-running it costs nothing.
        has_legacy_oidc_config_keys=has_auth_config
        and "auth_config_audit" in tables
        and _check_exists(
            "SELECT EXISTS(SELECT 1 FROM auth_config WHERE config_key LIKE 'keycloak\\_%' "
            "UNION ALL SELECT 1 FROM auth_config_audit WHERE config_key LIKE 'keycloak\\_%')"
        ),
        # v386: the tag_share table (sharing a tag with specific users/groups).
        has_tag_share="tag_share" in tables,
        # v387 guard: the five "who did this" FKs into `user` became ON DELETE SET NULL,
        # so deleting an admin who had ever changed auth config, quarantined someone
        # else's file, or shared someone else's prompt stops being a 500. Probed on
        # `auth_config_audit.changed_by` because that one ALSO lost its NOT NULL — it is
        # the marker that cannot be reached by a partial hand-repair of the others.
        # `confdeltype = 'n'` is Postgres's code for SET NULL.
        has_actor_fk_set_null=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'auth_config_audit_changed_by_fkey' AND confdeltype = 'n')"
        ),
        # Second half of the same revision: the CHECK v386 left off tag_share.target_type
        # while mirroring collection_share. Required as well as the FK marker, so a
        # database that somehow carries one and not the other re-runs the revision.
        has_tag_share_type_check=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = '_tag_share_target_type_check')"
        ),
        # Third half: the legacy duplicate of ck_user_role_valid is gone (v200 added it,
        # v387 drops it). An ABSENCE probe, like v385's — the same shape v380 used when it
        # removed the duplicate auth_type CHECK. Widening one of a pair and not the other is
        # a CheckViolation at login, not at migration time.
        has_legacy_role_check=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'users_role_check')"
        ),
        # v388: user_group carries the organization_id stamp, so the group plane finally has
        # a column a tenant filter can read. A single-marker revision (it only adds a column),
        # so the arm is v387's fingerprint plus this one probe.
        has_user_group_org=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user_group' AND column_name = 'organization_id')"
        ),
    )


# NOTE ON THE RENUMBERING. This branch's auth chain originally used v375-v381,
# branching off v374_add_tag_user_id independently of mainline's
# v375_add_chat_tables/v376_add_chat_projects (issue #52/#360) — both sides
# revised v374, producing two heads. Reconciled by renumbering this branch's
# seven revisions to v377-v383 (after master's chat chain) rather than
# renumbering master's, since master's chain had already reached production.
# Every revision id, detection arm, test file and doc reference below reflects
# the NEW numbers; nothing about the schema DDL itself changed.
ARMS: tuple[Arm, ...] = (
    # v388: same as v387 plus the group tenancy stamp.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
            and f.auth.has_oidc_subject
            and f.auth.has_oidc_user_refresh_token
            and f.auth.has_session_id_token
            and f.auth.has_user_approval_status
            and f.auth.has_approval_status_check
            and f.auth.has_scim_token
            and f.auth.has_proxy_group_source
            and f.auth.has_saml_subject
            and f.auth.has_saml_auth_type_check
            and f.auth.has_chat_reasoning_content
            and not f.auth.has_orphan_tables
            and f.auth.has_tag_share
            and f.auth.has_actor_fk_set_null
            and f.auth.has_tag_share_type_check
            and not f.auth.has_legacy_role_check
            and f.auth.has_user_group_org
        ),
        "v388_add_user_group_organization_id",
    ),
    # v387: the actor FKs are SET NULL and tag_share.target_type is constrained.
    # Everything v386 requires must also hold — this revision only alters existing
    # objects, so it is v386's fingerprint plus the two new rules.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
            and f.auth.has_oidc_subject
            and f.auth.has_oidc_user_refresh_token
            and f.auth.has_session_id_token
            and f.auth.has_user_approval_status
            and f.auth.has_approval_status_check
            and f.auth.has_scim_token
            and f.auth.has_proxy_group_source
            and f.auth.has_saml_subject
            and f.auth.has_saml_auth_type_check
            and f.auth.has_chat_reasoning_content
            and not f.auth.has_orphan_tables
            and f.auth.has_tag_share
            and f.auth.has_actor_fk_set_null
            and f.auth.has_tag_share_type_check
            and not f.auth.has_legacy_role_check
        ),
        "v387_actor_fks_and_tag_share_check",
    ),
    # v386: tag_share exists. Everything v385 requires must also hold — this
    # revision only adds, so it is v385's fingerprint plus the new table.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
            and f.auth.has_oidc_subject
            and f.auth.has_oidc_user_refresh_token
            and f.auth.has_session_id_token
            and f.auth.has_user_approval_status
            and f.auth.has_approval_status_check
            and f.auth.has_scim_token
            and f.auth.has_proxy_group_source
            and f.auth.has_saml_subject
            and f.auth.has_saml_auth_type_check
            and f.auth.has_chat_reasoning_content
            and not f.auth.has_orphan_tables
            and f.auth.has_tag_share
            # Mutual exclusion with the arm above, the same way v385 excludes v386: v387
            # only ALTERS objects v386 created, so without this both arms describe the
            # same database and the pair reads as ambiguous even though ordering saves it.
            and not (
                f.auth.has_actor_fk_set_null
                and f.auth.has_tag_share_type_check
                and not f.auth.has_legacy_role_check
            )
        ),
        "v386_add_tag_share",
    ),
    # v385: the three orphan tables are gone. Everything v384 requires must also
    # hold — this revision only subtracts, so it shares v384's fingerprint plus
    # the absence.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
            and f.auth.has_oidc_subject
            and f.auth.has_oidc_user_refresh_token
            and f.auth.has_session_id_token
            and f.auth.has_user_approval_status
            and f.auth.has_approval_status_check
            and f.auth.has_scim_token
            and f.auth.has_proxy_group_source
            and f.auth.has_saml_subject
            and f.auth.has_saml_auth_type_check
            and f.auth.has_chat_reasoning_content
            and not f.auth.has_orphan_tables
            # Without this, v385's arm matches a v386 database too and shadows it —
            # the ladder returns the FIRST match, and v386 only adds a table.
            and not f.auth.has_tag_share
        ),
        "v385_drop_orphan_tables",
    ),
    # v384: the chat_message.reasoning_content column (collapsible reasoning display).
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
            and f.auth.has_oidc_subject
            and f.auth.has_oidc_user_refresh_token
            and f.auth.has_session_id_token
            and f.auth.has_user_approval_status
            and f.auth.has_approval_status_check
            and f.auth.has_scim_token
            and f.auth.has_proxy_group_source
            and f.auth.has_saml_subject
            and f.auth.has_saml_auth_type_check
            and f.auth.has_chat_reasoning_content
        ),
        "v384_add_chat_reasoning_content",
    ),
    # v383: SAML auth-type CHECK widening + user.saml_subject.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
            and f.auth.has_oidc_subject
            and f.auth.has_oidc_user_refresh_token
            and f.auth.has_session_id_token
            and f.auth.has_user_approval_status
            and f.auth.has_approval_status_check
            and f.auth.has_scim_token
            and f.auth.has_proxy_group_source
            and f.auth.has_saml_subject
            and f.auth.has_saml_auth_type_check
        ),
        "v383_saml_auth_type",
    ),
    # v382: SCIM provisioning tokens + 'proxy'/'scim' group sources.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
            and f.auth.has_oidc_subject
            and f.auth.has_oidc_user_refresh_token
            and f.auth.has_session_id_token
            and f.auth.has_user_approval_status
            and f.auth.has_approval_status_check
            and f.auth.has_scim_token
            and f.auth.has_proxy_group_source
        ),
        "v382_scim_tokens",
    ),
    # v381: administrator approval state on user.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
            and f.auth.has_oidc_subject
            and f.auth.has_oidc_user_refresh_token
            and f.auth.has_session_id_token
            and f.auth.has_user_approval_status
            and f.auth.has_approval_status_check
        ),
        "v381_approval_state",
    ),
    # v380: OIDC identity columns and the auth_type value rename.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
            and f.auth.has_oidc_subject
            and f.auth.has_oidc_user_refresh_token
            and f.auth.has_session_id_token
        ),
        "v380_oidc_identity_columns",
    ),
    # v379: auth_config / auth_config_audit keys renamed to the oidc_ prefix.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
            and not f.auth.has_legacy_oidc_config_keys
        ),
        "v379_rename_keycloak_config_to_oidc",
    ),
    # v378: directory groups drive in-app groups and privileges.
    (
        lambda f: (
            matches_v376(f)
            and f.auth.has_auth_type_check
            and f.auth.has_user_invitation
            and f.auth.has_group_mapping
            and f.auth.has_membership_source
        ),
        "v378_idp_group_mapping",
    ),
    # v377: user auth invariants (role NOT NULL + auth_type CHECK) and the
    # invitation / email-verification schema.
    (
        lambda f: matches_v376(f) and f.auth.has_auth_type_check and f.auth.has_user_invitation,
        "v377_harden_user_auth_invariants",
    ),
)
