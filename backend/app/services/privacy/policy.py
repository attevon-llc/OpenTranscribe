"""One restrict-only privacy policy for voiceprints and inferred gender (issues #1280, #1279).

Two questions, answered for a (user, organization) scope: *may voiceprints exist and be
matched?* and *may gender be inferred?* Four layers are ANDed, in this order, and any layer that
is off turns the feature off:

1. **platform** - an extension resolver (``set_privacy_ceiling_resolver``); community: none.
2. **instance** - ``SystemSettings`` ``privacy.voiceprint_matching_enabled`` /
   ``privacy.gender_inference_enabled`` (defaults in ``core.constants``, both on).
3. **organization** - the ``organization_policy`` row; a missing row inherits everything.
4. **user** - ``UserSetting`` ``privacy_voiceprint_matching_enabled`` and the existing
   ``speaker_attribute_gender_detection_enabled``; a missing row inherits.

Voiceprint CREATION additionally asks the consent hooks (``consent.py``), reported as layer
``consent``. ``Decision.locked_by`` names the highest layer that turned the feature off, so a UI
can say who to ask.

Failure policy: **every error path denies.** A resolver that raises or returns the wrong shape, a
database read that fails, a file that no longer exists, a hook that raises: each reads as "off".
A policy that fails open is a policy that leaks the moment its storage hiccups.

Three enforcement tiers use this module (plan ``docs/design/v060_privacy_controls_plan.md`` D4):
the pipeline plan and entry gates call ``*_for_file`` (always a fresh read, no cache), and the
OpenSearch write functions call ``assert_voiceprint_write_allowed``, which caches for a few
seconds because it sits on every write.

Switching a layer off hides data and never deletes it; deletion is the separate, audited purge.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING
from typing import Literal

from app.core import constants
from app.core.env import is_truthy
from app.db.savepoint import isolated
from app.services.privacy.consent import VoiceprintConsentRequest
from app.services.privacy.consent import has_voiceprint_consent_hooks
from app.services.privacy.consent import voiceprint_consent_granted

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

Layer = Literal["platform", "instance", "organization", "user", "consent", "file"]


@dataclass(frozen=True)
class Decision:
    """Whether one feature is allowed, and the highest layer that forbade it.

    ``locked_by`` is ``None`` exactly when ``enabled`` is True. When a layer could not be
    read at all the feature is denied and ``locked_by`` names the layer that failed.
    """

    enabled: bool
    locked_by: Layer | None = None


@dataclass(frozen=True)
class PrivacyPolicy:
    """Effective policy of the four restrict-only layers for one scope.

    ``voiceprints`` here excludes the consent hooks, which need a purpose and a subject:
    use :func:`voiceprint_decision` when a voiceprint is about to be created.
    """

    voiceprints: Decision
    gender: Decision


@dataclass(frozen=True)
class PrivacyCeiling:
    """What the platform layer allows for one organization. ``None`` = no opinion.

    ``False`` can only restrict. ``True`` is the same as ``None``: a platform layer cannot
    switch on what the instance, the organization or the user switched off.
    """

    voiceprints: bool | None = None
    gender: bool | None = None


#: ``(db, organization_id) -> ceiling``. ``organization_id`` is None for personal scope. The
#: session is borrowed read-only, as for the redaction-floor resolver. It runs inside Celery
#: tasks with no request, so it must answer per organization and be fast.
PrivacyCeilingResolver = Callable[["Session", int | None], PrivacyCeiling | None]


def _community_ceiling_resolver(db: Session, organization_id: int | None) -> PrivacyCeiling | None:
    return None


_ceiling_resolver: PrivacyCeilingResolver = _community_ceiling_resolver


def set_privacy_ceiling_resolver(resolver: PrivacyCeilingResolver) -> None:
    """Replace the platform-layer resolver (registered by an extension at activation)."""
    global _ceiling_resolver
    logger.info("Privacy ceiling resolver overridden (extension)")
    _ceiling_resolver = resolver
    clear_write_backstop_cache()


def reset_privacy_ceiling_resolver() -> None:
    """Restore the community resolver, which has no opinion (primarily for tests)."""
    global _ceiling_resolver
    _ceiling_resolver = _community_ceiling_resolver
    clear_write_backstop_cache()


_DENY_PLATFORM = Decision(False, "platform")
_DENY_INSTANCE = Decision(False, "instance")


def _parse(raw: object) -> bool:
    return is_truthy(str(raw))


def _platform_layer(db: Session, organization_id: int | None) -> tuple[Decision, Decision, bool]:
    """The platform layer for (voiceprints, gender) and whether it answered cleanly.

    A resolver that raises or returns the wrong type denies both features and reports
    ``False`` so the write backstop does not cache the denial.
    """
    try:
        ceiling = _ceiling_resolver(db, organization_id)
    except Exception:
        logger.exception("privacy ceiling resolver failed; denying voiceprints and gender")
        return _DENY_PLATFORM, _DENY_PLATFORM, False
    if ceiling is None:
        return Decision(True), Decision(True), True
    if not isinstance(ceiling, PrivacyCeiling):
        logger.error("privacy ceiling resolver returned %r; denying", type(ceiling).__name__)
        return _DENY_PLATFORM, _DENY_PLATFORM, False

    def one(value: object) -> Decision:
        if value is None or value is True:
            return Decision(True)
        return _DENY_PLATFORM  # False, or a value of the wrong type

    return one(ceiling.voiceprints), one(ceiling.gender), True


def _first_off(*layers: tuple[Layer, bool]) -> Decision:
    for name, allowed in layers:
        if not allowed:
            return Decision(False, name)
    return Decision(True)


Scope = tuple[int | None, int | None]


def _stored_layers_many(db: Session, scopes: Iterable[Scope]) -> dict[Scope, dict[str, bool]]:
    """Instance, organization and user switches for many (user, organization) scopes.

    One statement per layer however many scopes are asked about (the instance row, the
    organization rows, the user rows), read fresh from the database.
    """
    from app.models.organization_policy import OrganizationPolicy
    from app.models.prompt import UserSetting
    from app.models.system_settings import SystemSettings

    wanted = list(dict.fromkeys(scopes))
    keys = (constants.PRIVACY_VOICEPRINT_MATCHING_KEY, constants.PRIVACY_GENDER_INFERENCE_KEY)
    rows = {
        str(r.key): r.value
        for r in db.query(SystemSettings.key, SystemSettings.value)
        .filter(SystemSettings.key.in_(keys))
        .all()
    }

    # Absent row = coded default. A present row that is NULL or unparseable reads as off.
    def instance_flag(key: str, default: bool) -> bool:
        if key not in rows:
            return default
        return rows[key] is not None and _parse(rows[key])

    instance_voiceprints = instance_flag(
        keys[0], constants.DEFAULT_PRIVACY_VOICEPRINT_MATCHING_ENABLED
    )
    instance_gender = instance_flag(keys[1], constants.DEFAULT_PRIVACY_GENDER_INFERENCE_ENABLED)

    org_ids = {o for _, o in wanted if o is not None}
    orgs: dict[int, tuple[bool, bool]] = {}
    if org_ids:
        orgs = {
            int(r[0]): (bool(r[1]), bool(r[2]))
            for r in db.query(
                OrganizationPolicy.organization_id,
                OrganizationPolicy.voiceprint_matching_enabled,
                OrganizationPolicy.gender_inference_enabled,
            )
            .filter(OrganizationPolicy.organization_id.in_(org_ids))
            .all()
        }

    user_keys = (
        constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
        constants.PRIVACY_USER_GENDER_DETECTION_KEY,
    )
    user_ids = {u for u, _ in wanted if u is not None}
    user_rows: dict[int, dict[str, object]] = {}
    if user_ids:
        for r in (
            db.query(UserSetting.user_id, UserSetting.setting_key, UserSetting.setting_value)
            .filter(UserSetting.user_id.in_(user_ids), UserSetting.setting_key.in_(user_keys))
            .all()
        ):
            user_rows.setdefault(int(r.user_id), {})[str(r.setting_key)] = r.setting_value

    def user_flag(user_id: int | None, key: str) -> bool:
        mine = user_rows.get(user_id, {}) if user_id is not None else {}
        return key not in mine or (mine[key] is not None and _parse(mine[key]))

    out: dict[Scope, dict[str, bool]] = {}
    for user_id, organization_id in wanted:
        org = orgs.get(organization_id) if organization_id is not None else None
        out[(user_id, organization_id)] = {
            "instance_voiceprints": instance_voiceprints,
            "instance_gender": instance_gender,
            "org_voiceprints": org[0] if org else True,
            "org_gender": org[1] if org else True,
            "user_voiceprints": user_flag(user_id, user_keys[0]),
            "user_gender": user_flag(user_id, user_keys[1]),
        }
    return out


def _stored_layers_isolated(db: Session, wanted: list[Scope]) -> dict[Scope, dict[str, bool]]:
    """:func:`_stored_layers_many` in a savepoint; a scope missing from the result is unreadable.

    When the batched read fails for several scopes, each scope is read again on its own, so a
    failure denies exactly the scopes a one-scope read would deny: a broken
    ``organization_policy`` table must not hide a personal scope, which never reads it. The
    retry only runs on the error path.
    """
    try:
        return isolated(db, lambda: _stored_layers_many(db, wanted))
    except Exception:
        logger.exception("privacy policy read failed; denying voiceprints and gender")
    if len(wanted) < 2:
        return {}
    out: dict[Scope, dict[str, bool]] = {}
    for scope in wanted:
        try:
            out.update(isolated(db, partial(_stored_layers_many, db, [scope])))
        except Exception:
            logger.exception("privacy policy read failed for scope %s; denying", scope)
    return out


def _resolve_many(db: Session, scopes: Iterable[Scope]) -> dict[Scope, tuple[PrivacyPolicy, bool]]:
    """The policy of each scope and whether every layer was read cleanly (False = an error denial)."""
    wanted = list(dict.fromkeys(scopes))
    platform = {org: _platform_layer(db, org) for org in {o for _, o in wanted}}
    stored_by_scope = _stored_layers_isolated(db, wanted)

    out: dict[Scope, tuple[PrivacyPolicy, bool]] = {}
    for scope in wanted:
        platform_vp, platform_gender, platform_ok = platform[scope[1]]
        stored = stored_by_scope.get(scope)
        if stored is None:
            out[scope] = (
                PrivacyPolicy(
                    voiceprints=platform_vp if not platform_vp.enabled else _DENY_INSTANCE,
                    gender=platform_gender if not platform_gender.enabled else _DENY_INSTANCE,
                ),
                False,
            )
            continue
        out[scope] = (
            PrivacyPolicy(
                voiceprints=_first_off(
                    ("platform", platform_vp.enabled),
                    ("instance", stored["instance_voiceprints"]),
                    ("organization", stored["org_voiceprints"]),
                    ("user", stored["user_voiceprints"]),
                ),
                gender=_first_off(
                    ("platform", platform_gender.enabled),
                    ("instance", stored["instance_gender"]),
                    ("organization", stored["org_gender"]),
                    ("user", stored["user_gender"]),
                ),
            ),
            platform_ok,
        )
    return out


def _resolve(
    db: Session, user_id: int | None, organization_id: int | None
) -> tuple[PrivacyPolicy, bool]:
    """The policy and whether every layer was read cleanly (False = a denial from an error)."""
    return _resolve_many(db, [(user_id, organization_id)])[(user_id, organization_id)]


def gender_decisions(db: Session, scopes: Iterable[Scope]) -> dict[Scope, Decision]:
    """:func:`gender_decision` for many scopes in a fixed number of statements."""
    return {scope: layers[0].gender for scope, layers in _resolve_many(db, scopes).items()}


def resolve_privacy_policy(
    db: Session, *, user_id: int | None, organization_id: int | None
) -> PrivacyPolicy:
    """Resolve the four layers for one scope. Never raises; an error denies.

    Args:
        db: Session, borrowed read-only. A failed read is isolated in a savepoint so the
            caller's transaction stays usable.
        user_id: The person whose switches apply (the file or profile owner), or None.
        organization_id: The tenant of the record, taken from its row, never from request
            state. None for personal scope, which never reads an organization row.

    Returns:
        The effective policy, excluding consent (see :func:`voiceprint_decision`).
    """
    return _resolve(db, user_id, organization_id)[0]


def _consent_allows(db: Session, request: VoiceprintConsentRequest | None) -> bool:
    """Consent hooks (each isolated in its own savepoint). ``None`` request = subject unknown."""
    if not has_voiceprint_consent_hooks():
        return True
    if request is None:
        return False
    try:
        return voiceprint_consent_granted(db, request)
    except Exception:
        logger.exception("voiceprint consent check failed; denying")
        return False


def _consent_required(db: Session) -> bool:
    """``privacy.voiceprint_upload_consent_required`` (absent = coded default, NULL = off)."""
    from app.models.system_settings import SystemSettings

    row = (
        db.query(SystemSettings.value)
        .filter(SystemSettings.key == constants.PRIVACY_VOICEPRINT_UPLOAD_CONSENT_REQUIRED_KEY)
        .first()
    )
    if row is None:
        return constants.DEFAULT_PRIVACY_VOICEPRINT_UPLOAD_CONSENT_REQUIRED
    return row[0] is not None and _parse(row[0])


def _file_consent_allows(db: Session, media_file_id: int) -> bool:
    """The file layer (issue #1356): restrict-only, and never cached.

    Denies a ``declined`` file, and a file that is not ``granted`` while the required setting
    is on. An unreadable file or setting denies. ``granted`` returns True here, which only means
    this layer does not object: the layers above have already been applied.
    """
    from app.models.media import MediaFile

    try:
        state = isolated(
            db,
            lambda: (
                db.query(MediaFile.voiceprint_consent)
                .filter(MediaFile.id == media_file_id)
                .scalar()
            ),
        )
        if state is None:
            logger.warning("privacy: file %s has no consent state; denying", media_file_id)
            return False
        if state == "declined":
            return False
        if state == "granted":
            return True
        return not isolated(db, lambda: _consent_required(db))
    except Exception:
        logger.exception("privacy: could not read the consent of file %s; denying", media_file_id)
        return False


def _voiceprint_decision(
    db: Session, user_id: int | None, organization_id: int | None, media_file_id: int | None
) -> tuple[Decision, bool]:
    layers, clean = _resolve(db, user_id, organization_id)
    return _voiceprint_after_layers(db, user_id, organization_id, media_file_id, layers, clean)


def _voiceprint_after_layers(
    db: Session,
    user_id: int | None,
    organization_id: int | None,
    media_file_id: int | None,
    layers: PrivacyPolicy,
    clean: bool,
) -> tuple[Decision, bool]:
    """The file layer and consent hooks, applied to already-resolved layers.

    Shared by the single and the batched decision so the two cannot drift apart.
    """
    if not layers.voiceprints.enabled:
        return layers.voiceprints, clean
    # File layer: after the stored layers, before the consent hooks (a cheap deny first).
    if media_file_id is not None and not _file_consent_allows(db, media_file_id):
        return Decision(False, "file"), clean
    request = (
        VoiceprintConsentRequest(
            purpose="create",
            subject_user_id=user_id,
            organization_id=organization_id,
            media_file_id=media_file_id,
        )
        if user_id is not None
        else None
    )
    if not _consent_allows(db, request):
        return Decision(False, "consent"), clean
    return Decision(True), clean


def voiceprint_decision(
    db: Session,
    *,
    user_id: int | None,
    organization_id: int | None,
    media_file_id: int | None = None,
) -> Decision:
    """May a voiceprint be CREATED for this scope? Layers plus the ``create`` consent hooks."""
    return _voiceprint_decision(db, user_id, organization_id, media_file_id)[0]


def voiceprint_decisions(db: Session, scopes: Iterable[Scope]) -> dict[Scope, Decision]:
    """:func:`voiceprint_decision` (no file) for many scopes, the layers in one batch.

    The four layers are read once for every scope (:func:`_resolve_many`); the consent hooks,
    when any are registered, are still asked once per scope whose layers allow, with the same
    ``create`` request the single-scope call builds. Each value equals
    ``voiceprint_decision(db, user_id=u, organization_id=o)``: the parity test in
    ``tests/unit/test_privacy_voiceprint_decisions_batched.py`` holds the two together.
    """
    return {
        scope: _voiceprint_after_layers(db, scope[0], scope[1], None, layers, clean)[0]
        for scope, (layers, clean) in _resolve_many(db, scopes).items()
    }


def gender_decision(db: Session, *, user_id: int | None, organization_id: int | None) -> Decision:
    """May gender be inferred for this scope?"""
    return resolve_privacy_policy(db, user_id=user_id, organization_id=organization_id).gender


def _file_scope(db: Session, file_id: int) -> tuple[int, int | None] | None:
    """(owner user id, organization id) of a file, from its row. None if unreadable."""
    from app.models.media import MediaFile

    try:
        row = isolated(
            db,
            lambda: (
                db.query(MediaFile.user_id, MediaFile.organization_id)
                .filter(MediaFile.id == file_id)
                .first()
            ),
        )
    except Exception:
        logger.exception("privacy: could not read the scope of file %s; denying", file_id)
        return None
    if row is None:
        logger.warning("privacy: file %s not found; denying", file_id)
        return None
    return int(row[0]), (int(row[1]) if row[1] is not None else None)


def voiceprint_decision_for_file(db: Session, file_id: int) -> Decision:
    """Voiceprint decision for a file's owner and organization (fresh read, no cache)."""
    scope = _file_scope(db, file_id)
    if scope is None:
        return _DENY_INSTANCE
    return voiceprint_decision(
        db, user_id=scope[0], organization_id=scope[1], media_file_id=file_id
    )


def gender_decision_for_file(db: Session, file_id: int) -> Decision:
    """Gender decision for a file's owner and organization (fresh read, no cache)."""
    scope = _file_scope(db, file_id)
    if scope is None:
        return _DENY_INSTANCE
    return gender_decision(db, user_id=scope[0], organization_id=scope[1])


def voiceprints_allowed_for_file(db: Session, file_id: int) -> bool:
    """Entry gate for every voiceprint producer and consumer working on a file."""
    return voiceprint_decision_for_file(db, file_id).enabled


def gender_allowed_for_file(db: Session, file_id: int) -> bool:
    """Entry gate for every gender producer and consumer working on a file."""
    return gender_decision_for_file(db, file_id).enabled


def filter_shareable_profiles(
    db: Session, requester_user_id: int, profile_ids: Iterable[int]
) -> list[int]:
    """Keep the profiles ``requester_user_id`` may match against, in input order.

    The requester's own profiles are kept. Another user's profile is dropped when that owner's
    scope (owner, and the profile's own organization) has voiceprints off, or when a ``share``
    consent hook denies. Hooks are asked once per (owner, organization), not per profile.
    Any error drops the non-owned profiles (fail closed).
    """
    from app.models.speaker import SpeakerProfile

    ids = list(dict.fromkeys(profile_ids))
    if not ids:
        return []
    try:
        rows = isolated(
            db,
            lambda: (
                db.query(SpeakerProfile.id, SpeakerProfile.user_id, SpeakerProfile.organization_id)
                .filter(SpeakerProfile.id.in_(ids))
                .all()
            ),
        )
    except Exception:
        logger.exception("privacy: profile ownership read failed; dropping shared profiles")
        return []

    owner_of = {int(r[0]): (int(r[1]), r[2] if r[2] is None else int(r[2])) for r in rows}
    groups: dict[tuple[int, int | None], list[int]] = {}
    for pid in ids:
        scope = owner_of.get(pid)
        if scope is not None and scope[0] != requester_user_id:
            groups.setdefault(scope, []).append(pid)

    allowed_groups: set[tuple[int, int | None]] = set()
    # One batched read for every group; _resolve_many never raises and an error denies.
    resolved = _resolve_many(db, groups.keys()) if groups else {}
    for (owner, org), group_ids in groups.items():
        if not resolved[(owner, org)][0].voiceprints.enabled:
            continue
        share = VoiceprintConsentRequest(
            purpose="share",
            subject_user_id=owner,
            organization_id=org,
            profile_ids=tuple(group_ids),
            requester_user_id=requester_user_id,
        )
        if _consent_allows(db, share):
            allowed_groups.add((owner, org))

    kept: list[int] = []
    for pid in ids:
        scope = owner_of.get(pid)
        if scope is None:
            continue  # unknown profile: nothing to vouch for
        if scope[0] == requester_user_id or scope in allowed_groups:
            kept.append(pid)
    return kept


# --- Tier 3: the write backstop ----------------------------------------------------------

_cache_lock = threading.Lock()
_write_cache: dict[tuple[int | None, int | None], tuple[float, bool]] = {}
_denial_logged: set[tuple[int | None, int | None]] = set()
_CACHE_MAX_ENTRIES = 10_000


def clear_write_backstop_cache() -> None:
    """Forget cached write decisions. Call after changing any layer in-process, and in tests."""
    with _cache_lock:
        _write_cache.clear()
        _denial_logged.clear()


def assert_voiceprint_write_allowed(
    user_id: int | None,
    organization_id: int | None,
    *,
    db: Session | None = None,
    media_file_id: int | None = None,
) -> bool:
    """Backstop for every OpenSearch voiceprint write. True = the write may proceed.

    The subject decision (owner and organization) is cached per scope. When the document names
    a file (``media_file_id``), that file's consent state is ALSO checked on every call, fresh
    and uncached, so an opt-out bites at once, and the ``create`` consent hooks receive the file
    id. A write with no file (a profile or cluster centroid) stays subject-scoped; the centroid
    computation excludes declined files instead (:func:`declined_file_ids`).

    Despite the name it returns instead of raising: a denied write logs once per scope and the
    caller returns its own existing "not written" value. The answer is cached per
    ``(user_id, organization_id)`` for ``PRIVACY_WRITE_BACKSTOP_TTL_SECONDS`` because this sits
    on every write, so a switch-off takes effect within that window in each process. Any error,
    including a resolver or storage error, denies and that denial is NOT cached.

    Args:
        user_id: Owner of the document being written.
        organization_id: Tenant of the document (from its row).
        db: A session the caller already holds, if any. Otherwise a short one is opened and
            closed before returning (never held across slow work).
    """
    if media_file_id is not None:
        return _write_allowed_for_file(user_id, organization_id, media_file_id, db)
    scope = (user_id, organization_id)
    now = time.monotonic()
    with _cache_lock:
        hit = _write_cache.get(scope)
        if hit is not None and hit[0] > now:
            return hit[1]

    try:
        if db is not None:
            decision, clean = _voiceprint_decision(db, user_id, organization_id, None)
        else:
            from app.db.session_utils import session_scope

            with session_scope() as short:
                decision, clean = _voiceprint_decision(short, user_id, organization_id, None)
    except Exception:
        logger.exception("privacy write backstop could not decide; denying (not cached)")
        return False

    with _cache_lock:
        if clean:
            if len(_write_cache) >= _CACHE_MAX_ENTRIES:
                _write_cache.clear()
                _denial_logged.clear()
            _write_cache[scope] = (
                now + constants.PRIVACY_WRITE_BACKSTOP_TTL_SECONDS,
                decision.enabled,
            )
        if not decision.enabled and scope not in _denial_logged:
            _denial_logged.add(scope)
            logger.warning(
                "voiceprint write refused by policy (locked_by=%s, user=%s, organization=%s)",
                decision.locked_by,
                user_id,
                organization_id,
            )
    return decision.enabled


def _write_allowed_for_file(
    user_id: int | None, organization_id: int | None, media_file_id: int, db: Session | None
) -> bool:
    """Subject decision (cached) AND the file layer (fresh) AND hooks given the file id."""
    if not assert_voiceprint_write_allowed(user_id, organization_id, db=db):
        return False
    try:
        if db is not None:
            decision, _ = _voiceprint_decision(db, user_id, organization_id, media_file_id)
        else:
            from app.db.session_utils import session_scope

            with session_scope() as short:
                decision, _ = _voiceprint_decision(short, user_id, organization_id, media_file_id)
    except Exception:
        logger.exception("privacy file write backstop could not decide; denying")
        return False
    if not decision.enabled:
        logger.warning(
            "voiceprint write refused for file %s (locked_by=%s)", media_file_id, decision.locked_by
        )
    return decision.enabled


def declined_file_ids(db: Session, file_ids: Iterable[int | None]) -> set[int]:
    """Which of these files must contribute NO voice to an average (fresh read, fails closed).

    A file is excluded when its consent denies voiceprints (``declined``, or not ``granted``
    while the required setting is on). An unreadable state excludes every file asked about.
    """
    from app.models.media import MediaFile

    ids = {int(i) for i in file_ids if i is not None}
    if not ids:
        return set()
    try:
        rows = isolated(
            db,
            lambda: (
                db.query(MediaFile.id, MediaFile.voiceprint_consent)
                .filter(MediaFile.id.in_(ids))
                .all()
            ),
        )
        required = isolated(db, lambda: _consent_required(db))
    except Exception:
        logger.exception("privacy: could not read file consent states; excluding all")
        return ids
    states = {int(r[0]): str(r[1]) for r in rows}
    return {
        i
        for i in ids
        if states.get(i) == "declined"
        or states.get(i) is None
        or (required and states[i] != "granted")
    }
