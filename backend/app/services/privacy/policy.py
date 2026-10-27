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
from typing import TYPE_CHECKING
from typing import Literal

from app.core import constants
from app.services.privacy.consent import VoiceprintConsentRequest
from app.services.privacy.consent import has_voiceprint_consent_hooks
from app.services.privacy.consent import voiceprint_consent_granted

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

Layer = Literal["platform", "instance", "organization", "user", "consent"]

_TRUTHY = ("true", "1", "yes", "on")


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


def _isolated[T](db: Session, read: Callable[[], T]) -> T:
    """Run ``read`` inside a savepoint so a failure cannot poison the caller's transaction.

    Written with an explicit ``rollback``/``commit`` rather than ``with db.begin_nested()``:
    the context-manager form raises "Can't operate on closed transaction" under the test
    harness's savepoint-restarting listener once the test has committed.
    """
    savepoint = db.begin_nested()
    try:
        result = read()
    except BaseException:
        savepoint.rollback()
        raise
    savepoint.commit()
    return result


_DENY_PLATFORM = Decision(False, "platform")
_DENY_INSTANCE = Decision(False, "instance")


def _parse(raw: object) -> bool:
    return str(raw).strip().lower() in _TRUTHY


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


def _stored_layers(
    db: Session, user_id: int | None, organization_id: int | None
) -> dict[str, bool]:
    """Instance, organization and user switches, read fresh from the database."""
    from app.models.organization_policy import OrganizationPolicy
    from app.models.prompt import UserSetting
    from app.models.system_settings import SystemSettings

    out: dict[str, bool] = {}
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

    out["instance_voiceprints"] = instance_flag(
        keys[0], constants.DEFAULT_PRIVACY_VOICEPRINT_MATCHING_ENABLED
    )
    out["instance_gender"] = instance_flag(
        keys[1], constants.DEFAULT_PRIVACY_GENDER_INFERENCE_ENABLED
    )

    out["org_voiceprints"] = True
    out["org_gender"] = True
    if organization_id is not None:
        org = (
            db.query(
                OrganizationPolicy.voiceprint_matching_enabled,
                OrganizationPolicy.gender_inference_enabled,
            )
            .filter(OrganizationPolicy.organization_id == organization_id)
            .first()
        )
        if org is not None:
            out["org_voiceprints"] = bool(org[0])
            out["org_gender"] = bool(org[1])

    out["user_voiceprints"] = True
    out["user_gender"] = True
    if user_id is not None:
        user_keys = (
            constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            constants.PRIVACY_USER_GENDER_DETECTION_KEY,
        )
        user_rows = {
            str(r.setting_key): r.setting_value
            for r in db.query(UserSetting.setting_key, UserSetting.setting_value)
            .filter(UserSetting.user_id == user_id, UserSetting.setting_key.in_(user_keys))
            .all()
        }
        if user_keys[0] in user_rows:
            out["user_voiceprints"] = user_rows[user_keys[0]] is not None and _parse(
                user_rows[user_keys[0]]
            )
        if user_keys[1] in user_rows:
            out["user_gender"] = user_rows[user_keys[1]] is not None and _parse(
                user_rows[user_keys[1]]
            )
    return out


def _resolve(
    db: Session, user_id: int | None, organization_id: int | None
) -> tuple[PrivacyPolicy, bool]:
    """The policy and whether every layer was read cleanly (False = a denial from an error)."""
    platform_vp, platform_gender, platform_ok = _platform_layer(db, organization_id)
    try:
        stored = _isolated(db, lambda: _stored_layers(db, user_id, organization_id))
    except Exception:
        logger.exception("privacy policy read failed; denying voiceprints and gender")
        return (
            PrivacyPolicy(
                voiceprints=platform_vp if not platform_vp.enabled else _DENY_INSTANCE,
                gender=platform_gender if not platform_gender.enabled else _DENY_INSTANCE,
            ),
            False,
        )
    return (
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


def _voiceprint_decision(
    db: Session, user_id: int | None, organization_id: int | None, media_file_id: int | None
) -> tuple[Decision, bool]:
    layers, clean = _resolve(db, user_id, organization_id)
    if not layers.voiceprints.enabled:
        return layers.voiceprints, clean
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


def gender_decision(db: Session, *, user_id: int | None, organization_id: int | None) -> Decision:
    """May gender be inferred for this scope?"""
    return resolve_privacy_policy(db, user_id=user_id, organization_id=organization_id).gender


def _file_scope(db: Session, file_id: int) -> tuple[int, int | None] | None:
    """(owner user id, organization id) of a file, from its row. None if unreadable."""
    from app.models.media import MediaFile

    try:
        row = _isolated(
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
    from app.models.media import SpeakerProfile

    ids = list(dict.fromkeys(profile_ids))
    if not ids:
        return []
    try:
        rows = _isolated(
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
    for (owner, org), group_ids in groups.items():
        layers = resolve_privacy_policy(db, user_id=owner, organization_id=org)
        if not layers.voiceprints.enabled:
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
    user_id: int | None, organization_id: int | None, *, db: Session | None = None
) -> bool:
    """Backstop for every OpenSearch voiceprint write. True = the write may proceed.

    The decision is per SUBJECT (owner and organization), never per file: a document is written
    for an owner, and no file is known at this point, so the ``create`` consent hooks receive
    ``media_file_id=None``. A hook that refuses one file only (see ``PrivacyView``, which does
    ask per file) therefore cannot stop this write backstop; the entry gates and the read side
    do.

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
