"""Every write path is classified for the legal-hold freeze, and a guarded one still guards.

A legal hold freezes a file's content in its existing state
(``docs/design/legal_hold_ux_freeze_plan.md`` §3): every content write refuses a held file
(``app/api/hold_refusal.refuse_if_frozen``, or a ``hold_guard`` predicate in a service) and every
background write skips a frozen one. This gate keeps a NEW write path from skipping that guard.

Two AST scanners, modelled on ``test_ws_event_quarantine_discipline.py``:

1. **Routes.** Every function under ``app/api/endpoints/**`` decorated with
   ``<...router>.{post,put,patch,delete}``.
2. **Celery tasks.** Every function decorated with ``<...>.task(`` under ``app/tasks/**`` and
   ``app/services/**``.

Each key (``"<path under app/>::<function>"``, never a line number) must sit in exactly one
table. A FREEZE-guarded entry names the CHAIN from the route or task to the function that calls
a guard: each link must call the next by name, and the last must call one of ``GUARD_CALLS``,
so a guard that is deleted, or a call that stops reaching it, fails here. A destroy-path entry
names the helper that guards it. Every exempt entry carries a written reason. A table entry
whose function no longer exists fails (stale).

Self-tested: an unguarded synthetic route must be reported, a guarded one must not, a stale
entry must be reported, and a chain whose middle link no longer calls the next must be
reported; without that, an empty result is indistinguishable from a clean tree.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.orm import Session

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: ``hold_guard``'s own answers to "is this file held / frozen".
GUARD_PRIMITIVES = frozenset(
    {
        "hold_state",
        "hold_states",
        "held_pred",
        "not_held_pred",
        "assert_not_held",
        "assert_content_mutable",
        "is_held",
        "is_frozen",
        "frozen_states",
        # A UNION spelling of held_pred for semi-joins (#1337 G8); kept equal to it by
        # tests/api/test_held_file_ids_matches_held_pred.py.
        "held_file_ids",
    }
)

#: Wrappers that count as a guard call. Each must itself call a primitive (pinned below).
GUARD_WRAPPERS: dict[str, str] = {
    "refuse_if_frozen": "api/hold_refusal.py::refuse_if_frozen",
    "refuse_reprocess_if_held": "api/hold_refusal.py::refuse_reprocess_if_held",
    "file_is_frozen": "tasks/hold_skip.py::file_is_frozen",
    "file_is_held": "tasks/hold_skip.py::file_is_held",
    "unheld_file_ids": "services/hold_guard.py::unheld_file_ids",
}

GUARD_CALLS = GUARD_PRIMITIVES | frozenset(GUARD_WRAPPERS)

#: Attribute calls that hand a task to Celery: ``extract_topics_task.delay(...)`` calls the task.
_DISPATCH_ATTRS = frozenset({"delay", "apply_async", "s", "si"})

_WRITE_METHODS = frozenset({"post", "put", "patch", "delete"})

_HINT = (
    "classify this write path for legal-hold freeze (see "
    "docs/design/legal_hold_ux_freeze_plan.md §4 and the tables in "
    "tests/unit/test_hold_freeze_coverage.py)"
)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@dataclass
class Index:
    """Every function of a source tree, the names it calls, and its decorators' shape."""

    calls: dict[str, set[str]] = field(default_factory=dict)
    attrs: dict[str, set[str]] = field(default_factory=dict)
    routes: set[str] = field(default_factory=set)
    tasks: set[str] = field(default_factory=set)


def _decorator_kind(dec: ast.expr) -> str | None:
    func = dec.func if isinstance(dec, ast.Call) else dec
    if not isinstance(func, ast.Attribute):
        return None
    base = func.value
    base_name = (
        base.id
        if isinstance(base, ast.Name)
        else base.attr
        if isinstance(base, ast.Attribute)
        else ""
    )
    if base_name.endswith("router") and func.attr in _WRITE_METHODS:
        return "route"
    if func.attr == "task" and isinstance(dec, ast.Call):
        return "task"
    return None


def _called_names(node: ast.AST) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    attrs: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            attrs.add(sub.attr)
        if not isinstance(sub, ast.Call):
            continue
        # A function handed over as an argument is called through (``result_writer=f``).
        for arg in [*sub.args, *(kw.value for kw in sub.keywords)]:
            if isinstance(arg, ast.Name):
                names.add(arg.id)
        func = sub.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
            if func.attr in _DISPATCH_ATTRS:
                target = func.value
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
    return names, attrs


def build_index(sources: Mapping[str, str]) -> Index:
    """Index ``{relpath: source}``. A function name repeated in one module merges its calls."""
    index = Index()
    for relpath, source in sources.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            key = f"{relpath}::{node.name}"
            names, attrs = _called_names(node)
            index.calls.setdefault(key, set()).update(names)
            index.attrs.setdefault(key, set()).update(attrs)
            for dec in node.decorator_list:
                kind = _decorator_kind(dec)
                if kind == "route" and relpath.startswith("api/endpoints/"):
                    index.routes.add(key)
                elif kind == "task" and relpath.startswith(("tasks/", "services/")):
                    index.tasks.add(key)
    return index


@functools.cache
def _app_index() -> Index:
    sources = {
        p.relative_to(_APP_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(_APP_ROOT.rglob("*.py"))
    }
    return build_index(sources)


def _fn(key: str) -> str:
    return key.rpartition("::")[2]


def chain_problems(index: Index, key: str, chain: tuple[str, ...]) -> list[str]:
    """Why ``key -> chain`` does not reach a guard call; empty when it does."""
    links = list(chain) if chain and chain[0] == key else [key, *chain]
    problems = [f"{key}: {link} does not exist" for link in links if link not in index.calls]
    if problems:
        return problems
    for caller, callee in zip(links, links[1:], strict=False):
        if _fn(callee) not in index.calls[caller]:
            problems.append(f"{key}: {caller} no longer calls {_fn(callee)}")
    if not index.calls[links[-1]] & GUARD_CALLS:
        problems.append(f"{key}: {links[-1]} calls no hold guard ({sorted(GUARD_CALLS)})")
    return problems


def _placement_problems(keys: set[str], tables: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    for key in sorted(keys):
        homes = [name for name, members in tables.items() if key in members]
        if not homes:
            problems.append(f"{key}: unclassified; {_HINT}")
        elif len(homes) > 1:
            problems.append(f"{key}: in more than one table {homes}")
    for name, members in tables.items():
        problems.extend(
            f"{key}: stale {name} entry, no such write path" for key in sorted(members - keys)
        )
    return problems


def _helper_problems(index: Index, key: str, helper: str, reason: str) -> list[str]:
    if not reason.strip():
        return [f"{key}: empty reason"]
    if helper not in index.calls:
        return [f"{key}: guard helper {helper} does not exist"]
    if not index.calls[helper] & GUARD_CALLS:
        return [f"{key}: guard helper {helper} calls no hold guard"]
    return []


def _annotation_problems(index: Index, key: str, chain: tuple[str, ...]) -> list[str]:
    links = list(chain) if chain[0] == key else [key, *chain]
    problems = [
        f"{key}: {caller} no longer calls {_fn(callee)}"
        for caller, callee in zip(links, links[1:], strict=False)
        if callee not in index.calls or _fn(callee) not in index.calls.get(caller, set())
    ]
    last = links[-1]
    staged = "stage_content_event" in index.calls.get(last, set())
    if not staged or "CONTENT_HOLD_ANNOTATION" not in index.attrs.get(last, set()):
        problems.append(f"{key}: {last} does not stage content.hold.annotation")
    return problems


def classify(
    keys: set[str],
    index: Index,
    *,
    guarded: Mapping[str, tuple[str, ...]],
    exempt: Mapping[str, str],
    helper_guarded: Mapping[str, tuple[str, str]] | None = None,
    annotated: Mapping[str, tuple[str, ...]] | None = None,
) -> list[str]:
    """Every problem with one scanner's classification; empty means the tree is clean."""
    helper_guarded = helper_guarded or {}
    annotated = annotated or {}
    problems = _placement_problems(
        keys,
        {
            "guarded": set(guarded),
            "exempt": set(exempt),
            "helper_guarded": set(helper_guarded),
            "annotated": set(annotated),
        },
    )
    for key, chain in guarded.items():
        if key in keys:
            problems.extend(chain_problems(index, key, chain))
    for key, (helper, reason) in helper_guarded.items():
        if key in keys:
            problems.extend(_helper_problems(index, key, helper, reason))
    for key, chain in annotated.items():
        if key in keys:
            problems.extend(_annotation_problems(index, key, chain))
    problems.extend(f"{key}: empty reason" for key, reason in exempt.items() if not reason.strip())
    return problems


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_E = "api/endpoints/"


def _r(path: str) -> str:
    return _E + path


#: Route -> chain to the guard (a 1-tuple of the route itself when it calls the guard directly).
ROUTE_FREEZE_GUARDED: dict[str, tuple[str, ...]] = {
    # W1 transcript segment text/times.
    _r("files/__init__.py::update_transcript_segment"): (
        _r("files/crud.py::update_single_transcript_segment"),
    ),
    # W2 segment speaker reassignment.
    _r("transcript_segments.py::update_segment_speaker"): (
        _r("transcript_segments.py::update_segment_speaker"),
    ),
    # W3 speaker merge (either side held).
    _r("speakers_merge.py::merge_speakers"): (_r("speakers_merge.py::merge_speakers"),),
    # W4, W5 speaker update / create.
    _r("speakers.py::update_speaker"): (_r("speakers.py::update_speaker"),),
    _r("speakers.py::create_speaker"): (_r("speakers.py::create_speaker"),),
    # W6, W7 verify / confirm gender.
    _r("speakers_suggestions.py::verify_speaker_identification"): (
        _r("speakers_suggestions.py::verify_speaker_identification"),
    ),
    _r("speakers_suggestions.py::confirm_speaker_gender"): (
        _r("speakers_suggestions.py::confirm_speaker_gender"),
    ),
    # W8 link a speaker to a profile.
    _r("speaker_profiles_matching.py::assign_speaker_to_profile"): (
        _r("speaker_profiles_matching.py::assign_speaker_to_profile"),
    ),
    # W9 every PUT /files/{uuid} field.
    _r("files/single_file.py::update_media_file_endpoint"): (
        _r("files/crud.py::update_media_file"),
    ),
    # W10-W12 summary generation and LLM speaker identification.
    # The checks run in a sync helper handed to run_in_threadpool (concurrency audit F5).
    _r("summarization.py::trigger_summarization"): (
        _r("summarization.py::_summarize_preconditions"),
    ),
    _r("summarization.py::identify_speakers"): (
        _r("summarization.py::_identify_speakers_preconditions"),
    ),
    _r("files/summary_status.py::retry_summary"): (
        _r("files/summary_status.py::_retry_preconditions"),
    ),
    # W13-W18 topics and auto-label.
    _r("topics.py::extract_topics"): (_r("topics.py::extract_topics"),),
    _r("topics.py::apply_topic_suggestions"): (_r("topics.py::apply_topic_suggestions"),),
    _r("topics.py::dismiss_topic_suggestions"): (_r("topics.py::dismiss_topic_suggestions"),),
    _r("topics.py::auto_label_single_file"): (_r("topics.py::auto_label_single_file"),),
    _r("topics.py::batch_extract_topics"): (_r("topics.py::batch_extract_topics"),),
    _r("topics.py::retroactive_auto_label"): (
        "tasks/auto_labeling.py::retroactive_auto_label_task",
        "tasks/auto_labeling.py::pending_suggestion_ids",
    ),
    # W19-W23 tags (the service backstop is shared with the bulk rail).
    _r("tags/crud.py::add_tag_to_file"): (_r("tags/crud.py::add_tag_to_file"),),
    _r("tags/crud.py::remove_tag_from_file"): (_r("tags/crud.py::remove_tag_from_file"),),
    _r("tags/operations.py::merge_tags_endpoint"): (
        "services/tag_operations.py::merge_tags",
        "services/tag_operations.py::_fold",
        "services/tag_operations.py::_move_associations",
    ),
    _r("tags/operations.py::delete_tags_endpoint"): ("services/tag_operations.py::delete_tags",),
    _r("tags/operations.py::promote_tags_endpoint"): (
        "services/tag_operations.py::promote_tags_to_shared",
        "services/tag_operations.py::_fold",
        "services/tag_operations.py::_move_associations",
    ),
    # A rename onto another tag's name with ``confirm_merge`` is a merge.
    _r("tags/operations.py::rename_tag_endpoint"): (
        "services/tag_operations.py::rename_tag",
        "services/tag_operations.py::merge_tags",
        "services/tag_operations.py::_fold",
        "services/tag_operations.py::_move_associations",
    ),
    # W24-W26 collections.
    _r("media_collections_membership.py::add_media_to_collection"): (
        _r("media_collections_membership.py::add_media_to_collection"),
    ),
    _r("media_collections_membership.py::remove_media_from_collection"): (
        _r("media_collections_membership.py::remove_media_from_collection"),
    ),
    _r("media_collections.py::delete_collection"): (_r("media_collections.py::delete_collection"),),
    # W27, W28 existing comments.
    _r("comments.py::update_comment"): (_r("comments.py::_refuse_if_comment_frozen"),),
    _r("comments.py::delete_comment"): (_r("comments.py::_refuse_if_comment_frozen"),),
    # W29-W32 and W50: every bulk verb that changes a file, through one gate.
    _r("files/bulk_actions.py::bulk_file_action"): (
        _r("files/bulk_actions.py::_process_single_file_action"),
        _r("files/bulk_action_handlers.py::bulk_hold_refusal"),
    ),
    # W33-W35 recover / retry.
    _r("files/management.py::recover_file"): (_r("files/management.py::recover_file"),),
    _r("tasks_file_repair.py::retry_file_processing"): (
        _r("tasks_file_repair.py::retry_file_processing"),
    ),
    _r("user_files.py::retry_file_processing"): (_r("user_files.py::retry_file_processing"),),
    # W36-W44 cross-file speaker operations: held rows are excluded or the call is refused.
    _r("speaker_profiles.py::update_speaker_profile"): (
        "services/speaker_profile_rename.py::apply_profile_name_to_speakers",
    ),
    _r("speaker_profiles.py::delete_speaker_profile"): (
        _r("speaker_profiles.py::delete_speaker_profile"),
    ),
    _r("speaker_profiles_attributes.py::confirm_profile_gender"): (
        "services/speaker_label_queries.py::set_gender_on_profile_speakers",
    ),
    _r("speaker_clusters.py::batch_verify"): (
        "services/clustering/verification.py::batch_verify_speakers",
    ),
    _r("speaker_clusters.py::unassign_speakers"): (
        "services/clustering/cluster_identity.py::unassign_speakers",
    ),
    _r("speaker_clusters.py::promote_cluster"): (
        "services/clustering/cluster_identity.py::promote_cluster_to_profile",
    ),
    _r("speaker_clusters.py::merge_clusters"): (
        "services/clustering/admin_ops.py::merge_clusters",
    ),
    _r("speaker_clusters.py::split_cluster"): ("services/clustering/admin_ops.py::split_cluster",),
    # W45 the admin redaction reindex sweep.
    _r("redaction_settings.py::trigger_redaction_reindex"): (
        "tasks/redaction_task.py::redaction_reindex_all_task",
    ),
    # W46-W49, W51: guarded before the freeze, now through the shared helper.
    _r("speakers.py::delete_speaker"): (_r("speakers.py::delete_speaker"),),
    _r("summarization.py::delete_summary"): (_r("summarization.py::delete_summary"),),
    _r("files/__init__.py::reprocess_media_file"): (
        _r("files/reprocess.py::process_file_reprocess"),
    ),
    _r("files/management.py::retry_file_processing"): (
        _r("files/management.py::retry_file_processing"),
    ),
    _r("files/voiceprint_consent.py::set_file_voiceprint_consent"): (
        _r("files/voiceprint_consent.py::set_file_voiceprint_consent"),
    ),
}

#: W52, W53: a new comment on a held file is allowed and staged as ``content.hold.annotation``.
ROUTE_ALLOWED_ON_HELD: dict[str, tuple[str, ...]] = {
    _r("comments.py::create_comment_for_file_nested"): (
        _r("comments.py::_annotate_if_held"),
        "services/content_hold_service.py::record_hold_annotation",
    ),
    _r("comments.py::create_comment_standalone"): (
        _r("comments.py::_annotate_if_held"),
        "services/content_hold_service.py::record_hold_annotation",
    ),
}

_DESTROY = "a destroy path, refused for a held file before the freeze existed"
#: Destroy paths: (the helper that guards, reason).
ROUTE_DESTROY_GUARDED: dict[str, tuple[str, str]] = {
    _r("files/cancel_upload.py::cancel_upload"): (_r("files/crud.py::delete_media_file"), _DESTROY),
    _r("files/management.py::force_delete_file"): (
        _r("files/crud.py::delete_media_file"),
        _DESTROY,
    ),
    _r("files/trash.py::purge_file"): (_r("files/trash.py::purge_file"), _DESTROY),
    _r("files/trash.py::empty_trash"): ("services/trash_purge.py::_due", _DESTROY),
    _r("erasure_receipts.py::admin_erase_file"): (
        "services/file_cleanup_service.py::purge_media_file",
        _DESTROY + " (single-recording erasure is deferred while held, Art. 17(3)(e))",
    ),
    _r("erasure_receipts.py::org_erase_file"): (
        "services/file_cleanup_service.py::purge_media_file",
        _DESTROY + " (single-recording erasure is deferred while held, Art. 17(3)(e))",
    ),
    _r("admin_maintenance.py::admin_erase_user"): (
        "services/gdpr_erasure_steps.py::_purge_files",
        _DESTROY + "; erasure defers held files and comments on them (Art. 17(3)(e))",
    ),
    _r("self_erasure.py::request_erasure"): (
        "services/gdpr_erasure_steps.py::_purge_files",
        _DESTROY + "; the scheduled erasure defers held files and comments on them",
    ),
    _r("org_admin_erasure.py::erase_org_member"): (
        "services/gdpr_erasure_steps.py::_delete_comments_deferring_held",
        _DESTROY + "; the scheduled member erasure defers held files and comments on them",
    ),
    _r("org_admin_erasure.py::erase_own_organization"): (
        "services/gdpr_erasure_steps.py::_purge_files",
        _DESTROY + "; an organization hold defers the organization erasure",
    ),
    _r("admin.py::delete_admin_user"): (
        "services/user_deletion_service.py::_assert_no_held_files",
        _DESTROY + "; refused while the account owns or commented on a held file",
    ),
    _r("users_admin_edit.py::delete_user"): (
        "services/user_deletion_service.py::_assert_no_held_files",
        _DESTROY + "; refused while the account owns or commented on a held file",
    ),
    _r("scim/users.py::delete_user"): (
        "services/user_deletion_service.py::_assert_no_held_files",
        _DESTROY + "; refused while the account owns or commented on a held file",
    ),
    _r("privacy.py::purge_my_data"): (
        "services/privacy/purge_file_guard.py::unprotected_file_pred",
        _DESTROY + " (a privacy purge leaves held files' data in place)",
    ),
    _r("privacy.py::purge_instance_data"): (
        "services/privacy/purge_file_guard.py::unprotected_file_pred",
        _DESTROY + " (a privacy purge leaves held files' data in place)",
    ),
    _r("org_admin_policy.py::purge_org_data"): (
        "services/privacy/purge_file_guard.py::unprotected_file_pred",
        _DESTROY + " (a privacy purge leaves held files' data in place)",
    ),
    _r("admin_settings.py::trigger_retention_run"): (
        "tasks/cleanup.py::retention_guard_pred",
        _DESTROY + " (retention never selects a held file)",
    ),
}

_SETTINGS = "settings or configuration: no file's content"
_AUTH = "authentication, session or identity: no file's content"
_ACCOUNTS = "account, group or role administration: no file's content"
_CHAT = "the caller's own chat threads, messages and projects: not a file's record"
_PROMPTS = "summary prompts and their sharing: no file's content"
_VOCAB = "custom vocabulary terms: no file's content"
_SUPPORT = "support-access grants: access, not content"
_HOLDS = "placing or releasing a hold or a takedown: the hold itself, not the file's content"
_UPLOAD = "creates a new file or upload session: there is no existing (held) file to change"
_DERIVED = (
    "regenerates a derived copy of the record (search index, embeddings, caches, analytics, "
    "waveform, export): allowed on a held file (plan §3.1)"
)
_EXPORT = "an export or download: reading a held file stays allowed (plan §3.1)"
_LABELS = (
    "renames, describes, shares or changes the visibility of a collection or tag: labels and "
    "access, not a file's record (plan §3.1)"
)
_VIEW = (
    "the caller's own per-user view preference (hide / unhide): not a content edit, never "
    "frozen (L5)"
)
_RESTORE = (
    "restores a file from the trash: visibility, not content; a held file in the trash stays "
    "restorable (1091 §4, plan §3.1)"
)
_IN_FLIGHT = (
    "acts only on a file still in its initial processing run (cancel, stuck-task recovery, "
    "status repair); a re-run it triggers goes through reset_file_for_retry, which refuses a "
    "held file"
)
_PROFILE = "a speaker profile or collection's own fields: no file's speaker is written"
_CLUSTER_GROUPING = (
    "cluster grouping only (label, description, cluster_id); no speaker name, profile link or "
    "verification of a held file is written (plan W40/W41, verified)"
)
_WATCH = "watch-source configuration and its tracking rows: not a media file's record"
_MIGRATION = "admin migration or maintenance control of derived data (embeddings, attributes)"
_ORPHANS = "data-integrity sweep of rows/objects with no live media file"
_NOTICE = "takedown notice records: the notice, not the file's content"
_ERASURE_CONTROL = "cancels or configures an erasure request: destroys nothing"

ROUTE_NOT_FILE_CONTENT: dict[str, str] = {
    **{
        _r(k): _SETTINGS
        for k in (
            "admin_audit_content_access.py::set_content_access_audit",
            "admin_group_mappings.py::create_group_mapping",
            "admin_group_mappings.py::update_group_mapping",
            "admin_group_mappings.py::delete_group_mapping",
            "admin_group_mappings.py::test_group_mapping",
            "admin_hf_token.py::set_hf_token",
            "admin_hf_token.py::clear_hf_token",
            "admin_hf_token.py::test_hf_token",
            "admin_llm_budget.py::update_llm_budget",
            "admin_maintenance.py::update_system_ai_summary_setting",
            "admin_settings.py::update_retry_configuration",
            "admin_settings.py::update_garbage_cleanup_configuration",
            "admin_settings.py::update_retention_configuration",
            "admin_settings.py::update_cache_configuration",
            "admin_settings.py::add_media_source",
            "admin_settings.py::update_media_source",
            "admin_settings.py::delete_media_source",
            "admin_telemetry.py::set_telemetry_enabled",
            "alertmanager_webhook.py::alertmanager_webhook",
            "asr_connection_tests.py::test_asr_connection",
            "asr_connection_tests.py::test_saved_asr_config",
            "asr_local_model.py::set_active_local_model",
            "asr_local_model.py::restart_gpu_worker",
            "asr_provider_policy.py::put_provider_policy",
            "asr_settings.py::create_asr_config",
            "asr_settings.py::update_asr_config",
            "asr_settings.py::set_active_asr_config",
            "asr_settings.py::delete_asr_config",
            "asr_settings.py::clear_active_asr_config",
            "asr_settings.py::delete_all_asr_configs",
            "auth_config.py::update_config_category",
            "auth_config.py::migrate_from_env",
            "auth_config_connection_tests.py::test_auth_connection",
            "auth_email_delivery.py::update_auth_mail_designation",
            "backup_settings.py::update_backup_settings",
            "backup_settings.py::test_s3_connection",
            "backup_settings.py::run_backup_now",
            "chat/admin_settings.py::update_chat_admin_settings",
            "chat/user_settings.py::update_chat_user_settings",
            "chat/user_settings.py::reset_chat_user_settings",
            "diarization_settings.py::save_pyannote_credential",
            "diarization_settings.py::delete_pyannote_credential",
            "diarization_settings.py::test_pyannote_credential",
            "directory_sync_settings.py::update_directory_sync_settings",
            "directory_sync_settings.py::run_directory_sync_now",
            "engine_settings.py::update_engine_settings",
            "engine_settings.py::reset_engine_setting",
            "erasure_receipts.py::write_ignored_indices",
            "first_run_wizard.py::complete_first_run_wizard",
            "legal.py::acknowledge_legal_document",
            "legal_processing_gate.py::put_processing_gate_settings",
            "llm_connection_tests.py::test_llm_connection",
            "llm_connection_tests.py::test_active_configuration",
            "llm_connection_tests.py::test_specific_configuration",
            "llm_connection_tests.py::probe_reasoning_capability",
            "llm_connection_tests.py::probe_context_window",
            "llm_settings.py::create_user_llm_configuration",
            "llm_settings.py::update_user_llm_configuration",
            "llm_settings.py::set_active_configuration",
            "llm_settings.py::delete_user_llm_configuration",
            "llm_settings.py::delete_all_user_configurations",
            "llm_status.py::test_llm_connection",
            "local_models.py::start_local_model_download",
            "local_models.py::cancel_local_model_download",
            "media_mirror_settings.py::update_mirror_settings",
            "media_mirror_settings.py::test_mirror_s3_connection",
            "media_mirror_settings.py::run_mirror_now",
            "media_private_hosts.py::put_private_hosts",
            "org_admin_policy.py::patch_org_policy",
            "privacy.py::put_my_privacy",
            "privacy.py::put_instance_privacy_policy",
            "privacy.py::reverify_pending_purges",
            "redaction_settings.py::update_redaction_settings",
            "redaction_settings.py::reset_redaction_settings",
            "redaction_settings.py::update_redaction_policy",
            "search_models.py::set_embedding_model",
            "search_models.py::register_neural_model",
            "search_models.py::deploy_neural_model",
            "search_models.py::undeploy_neural_model",
            "search_models.py::set_active_neural_model",
            "self_erasure.py::put_self_erasure_settings",
            "transcription_defaults.py::put_transcription_defaults",
            "user_settings_ai_features.py::update_auto_label_settings",
            "user_settings_ai_features.py::update_ai_summary_setting",
            "user_settings_audio_extraction.py::update_audio_extraction_settings",
            "user_settings_download.py::update_download_settings",
            "user_settings_download.py::reset_download_settings",
            "user_settings_media_sources.py::create_media_source",
            "user_settings_media_sources.py::update_media_source",
            "user_settings_media_sources.py::delete_media_source",
            "user_settings_organization_context.py::update_organization_context",
            "user_settings_organization_context.py::reset_organization_context",
            "user_settings_organization_context.py::use_shared_organization_context",
            "user_settings_recording.py::update_recording_settings",
            "user_settings_recording.py::reset_recording_settings",
            "user_settings_speaker_attributes.py::update_speaker_attribute_settings",
            "user_settings_speaker_attributes.py::reset_speaker_attribute_settings",
            "user_settings_transcription.py::update_transcription_settings",
            "user_settings_transcription.py::reset_transcription_settings",
            "watch_sources_settings.py::test_multipart_regex",
            "watch_sources_settings.py::update_global_settings",
            "youtube_settings.py::update_youtube_settings",
        )
    },
    **{
        _r(k): _AUTH
        for k in (
            "auth/demo_session.py::start_demo_session",
            "auth/email_verification.py::verify_email_endpoint",
            "auth/email_verification.py::resend_verification_endpoint",
            "auth/invitations.py::create_invitation_endpoint",
            "auth/invitations.py::revoke_invitation_endpoint",
            "auth/invitations.py::lookup_invitation_endpoint",
            "auth/invitations.py::accept_invitation_endpoint",
            "auth/login.py::login_for_access_token",
            "auth/methods.py::acknowledge_banner",
            "auth/mfa.py::setup_mfa",
            "auth/mfa.py::verify_mfa_setup",
            "auth/mfa.py::verify_mfa",
            "auth/mfa.py::disable_mfa",
            "auth/pki.py::pki_login",
            "auth/proxy.py::proxy_login",
            "auth/registration.py::register",
            "auth/registration.py::request_password_reset_endpoint",
            "auth/registration.py::confirm_password_reset_endpoint",
            "auth/saml.py::saml_acs",
            "auth/saml.py::saml_sls",
            "auth/sessions.py::refresh_access_token",
            "auth/sessions.py::logout",
            "auth/sessions.py::logout_all_sessions",
            "admin_identity.py::admin_link_external_identity",
            "admin_identity.py::admin_update_external_email",
            "admin_identity.py::admin_reset_user_mfa",
            "admin_scim_tokens.py::create_scim_token",
            "admin_scim_tokens.py::revoke_scim_token",
        )
    },
    **{
        _r(k): _ACCOUNTS
        for k in (
            "admin.py::create_admin_user",
            "admin_accounts.py::admin_reset_user_password",
            "admin_accounts.py::admin_unlock_account",
            "admin_accounts.py::admin_reset_lockout_counter",
            "admin_accounts.py::admin_lock_account",
            "admin_accounts.py::admin_terminate_user_sessions",
            "admin_accounts.py::admin_change_user_role",
            "admin_approvals.py::approve_account",
            "admin_approvals.py::reject_account",
            "groups.py::create_group",
            "groups.py::update_group",
            "groups.py::delete_group",
            "groups.py::add_member",
            "groups.py::update_member_role",
            "groups.py::remove_member",
            "scim/groups.py::create_group",
            "scim/groups.py::replace_group",
            "scim/groups.py::patch_group",
            "scim/groups.py::delete_group",
            "scim/users.py::create_user",
            "scim/users.py::replace_user",
            "scim/users.py::patch_user",
            "users_admin_edit.py::update_user",
            "users_self_service.py::update_current_user",
        )
    },
    **{
        _r(k): _CHAT
        for k in (
            "chat/conversations.py::create_conversation",
            "chat/conversations.py::update_conversation",
            "chat/conversations.py::delete_conversation",
            "chat/messages.py::send_message",
            "chat/messages.py::regenerate",
            "chat/messages.py::edit_message",
            "chat/messages.py::cancel_message",
            "chat/messages.py::estimate_context",
            "chat/projects.py::create_project",
            "chat/projects.py::update_project",
            "chat/projects.py::delete_project",
        )
    },
    **{
        _r(k): _PROMPTS
        for k in (
            "prompts.py::create_prompt",
            "prompts.py::update_prompt",
            "prompts.py::delete_prompt",
            "prompts.py::clone_prompt",
            "prompts_active.py::set_active_prompt",
            "prompts_sharing.py::share_prompt",
        )
    },
    **{
        _r(k): _VOCAB
        for k in (
            "custom_vocabulary.py::create_vocabulary_term",
            "custom_vocabulary.py::update_vocabulary_term",
            "custom_vocabulary.py::delete_all_user_vocabulary",
            "custom_vocabulary.py::delete_vocabulary_term",
            "custom_vocabulary.py::bulk_import_vocabulary",
        )
    },
    **{
        _r(k): _SUPPORT
        for k in (
            "support_access.py::create_grant",
            "support_access.py::create_break_glass_grant",
            "support_access.py::revoke_grant",
            "support_access.py::approve_org_grant",
            "support_access.py::deny_org_grant",
            "support_access.py::approve_my_workspace_grant",
            "support_access.py::deny_my_workspace_grant",
        )
    },
    **{
        _r(k): _HOLDS
        for k in (
            "admin_holds.py::place_admin_hold",
            "admin_holds.py::release_admin_hold",
            "org_admin_holds.py::place_org_hold",
            "org_admin_holds.py::release_org_hold",
            "admin_quarantine.py::quarantine_media_file",
            "admin_quarantine.py::release_media_file",
            "org_admin_takedown.py::quarantine_org_file",
            "org_admin_takedown.py::release_org_file",
        )
    },
    **{
        _r(k): _NOTICE
        for k in (
            "admin_takedown_notices.py::create_takedown_notice",
            "admin_takedown_notices.py::update_takedown_notice_file",
            "admin_takedown_notices.py::notify_takedown_notice",
        )
    },
    **{
        _r(k): _UPLOAD
        for k in (
            "files/__init__.py::upload_media_file",
            "files/complete_upload.py::complete_upload",
            "files/multipart.py::sign_upload_parts",
            "files/prepare_upload.py::prepare_upload",
            "files/url_processing.py::process_media_url",
        )
    },
    **{
        _r(k): _DERIVED
        for k in (
            "files/__init__.py::clear_video_cache",
            "files/__init__.py::refresh_analytics",
            "files/waveform.py::generate_waveform_for_file",
            "files/waveform.py::generate_waveforms_for_files",
            "search_index_health.py::repair_indices",
            "search_reindex.py::trigger_reindex",
            "search_reindex.py::stop_reindex",
            "search_reindex.py::trigger_reembed_degraded",
            "admin_settings.py::clear_cache_now",
            "admin_maintenance.py::repair_profile_embeddings",
            "admin_maintenance.py::start_embedding_consistency_repair",
            "admin_maintenance.py::stop_embedding_consistency_repair",
            "admin_maintenance.py::start_imohash_recompute",
            "speakers_debug.py::cleanup_orphaned_embeddings",
            "speaker_clusters.py::trigger_recluster",
            "speaker_clusters.py::analyze_outliers",
        )
    },
    **{
        _r(k): _EXPORT
        for k in (
            "files/media_delivery.py::prepare_download",
            "files/subtitles.py::prepare_bulk_export",
            "files/transcript_document_export.py::request_document_export",
        )
    },
    **{
        _r(k): _LABELS
        for k in (
            "media_collections.py::create_collection",
            "media_collections.py::update_collection",
            "media_collections.py::set_collection_visibility",
            "media_collections_shares.py::create_collection_share",
            "media_collections_shares.py::update_collection_share",
            "media_collections_shares.py::delete_collection_share",
            "tags/crud.py::create_tag",
            "tags/sharing.py::create_tag_share",
            "tags/sharing.py::revoke_tag_share",
        )
    },
    _r("tags/crud.py::cleanup_unused_tags"): (
        "deletes tags no file carries: an unused tag has no file link, so no held file changes"
    ),
    _r("files/hidden.py::hide_file"): _VIEW,
    _r("files/hidden.py::unhide_file"): _VIEW,
    _r("files/trash.py::restore_file"): _RESTORE,
    **{
        _r(k): _IN_FLIGHT
        for k in (
            "files/management.py::cancel_file_processing",
            "files/management.py::recover_stuck_files_bulk",
            "tasks_file_repair.py::fix_inconsistent_file",
            "tasks_file_repair.py::fix_all_inconsistent_files",
            "tasks_recovery.py::recover_all_stuck_tasks",
            "tasks_recovery.py::trigger_startup_recovery",
            "tasks_recovery.py::trigger_all_user_file_recovery",
            "tasks_recovery.py::trigger_user_file_recovery",
            "tasks_recovery.py::recover_task",
            "user_files.py::request_user_recovery",
        )
    },
    **{
        _r(k): _PROFILE
        for k in (
            "speaker_profiles.py::create_speaker_profile",
            "speaker_profiles_attributes.py::upload_profile_avatar",
            "speaker_profiles_attributes.py::delete_profile_avatar",
            "speaker_profiles_collections.py::create_speaker_collection",
        )
    },
    _r("speaker_clusters.py::update_cluster"): _CLUSTER_GROUPING,
    _r("speaker_clusters.py::delete_cluster"): _CLUSTER_GROUPING,
    **{
        _r(k): _WATCH
        for k in (
            "watch_sources.py::create_watch_source",
            "watch_sources.py::update_watch_source",
            "watch_sources.py::delete_watch_source",
            "watch_sources.py::test_watch_source",
            "watch_sources.py::scan_watch_source",
            "watch_sources_email_configs.py::create_email_config",
            "watch_sources_email_configs.py::update_email_config",
            "watch_sources_email_configs.py::delete_email_config",
            "watch_sources_email_configs.py::test_email_config",
            "watch_sources_email_links.py::link_email_config",
            "watch_sources_email_links.py::unlink_email_config",
            "watch_sources_files.py::delete_source_file",
            "watch_sources_files.py::bulk_delete_source_files",
            "watch_sources_files.py::retry_source_files",
        )
    },
    **{
        _r(k): _MIGRATION
        for k in (
            "admin_maintenance.py::start_media_duration_backfill",
            "combined_speaker_migration.py::start_combined_migration",
            "combined_speaker_migration.py::stop_combined_migration",
            "combined_speaker_migration.py::clear_combined_progress",
            "embedding_migration.py::start_migration",
            "embedding_migration.py::stop_migration",
            "embedding_migration.py::finalize_migration",
            "embedding_migration.py::clear_progress",
            "embedding_migration.py::retry_failed_files",
            "embedding_migration.py::force_complete_migration",
            "speaker_attribute_migration.py::start_attribute_migration",
            "speaker_attribute_migration.py::stop_attribute_migration",
            "speaker_attribute_migration.py::clear_attribute_progress",
        )
    },
    _r("admin_maintenance.py::start_data_integrity_check"): _ORPHANS,
    _r("admin_storage_integrity.py::start_storage_orphan_sweep"): _ORPHANS,
    _r("org_admin_erasure.py::cancel_scheduled_erasure"): _ERASURE_CONTROL,
    _r("org_admin_ownership.py::transfer_ownership"): (
        "hands the organization's owner role to another admin: writes the organization row only"
    ),
    _r("self_erasure.py::cancel_erasure"): _ERASURE_CONTROL,
}


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------

#: Task -> chain to the guard.
TASK_FREEZE_GUARDED: dict[str, tuple[str, ...]] = {
    # T1-T3 LLM writes.
    "tasks/summarization.py::summarize_transcript_task": (
        "tasks/summarization.py::summarize_transcript_task",
    ),
    "tasks/topic_extraction.py::extract_topics_task": (
        "tasks/topic_extraction.py::extract_topics_task",
    ),
    "tasks/topic_extraction.py::batch_extract_topics_task": (
        "tasks/topic_extraction.py::extract_topics_task",
    ),
    "tasks/speaker_identification_task.py::identify_speakers_llm_task": (
        "tasks/speaker_identification_task.py::identify_speakers_llm_task",
    ),
    # T4 auto-label.
    "tasks/auto_labeling.py::group_batch_files_task": (
        "services/auto_label_service.py::group_batch_by_topics",
    ),
    "tasks/auto_labeling.py::retroactive_auto_label_task": (
        "tasks/auto_labeling.py::pending_suggestion_ids",
    ),
    "tasks/auto_labeling.py::auto_label_batch_task": (
        "services/auto_label_service.py::auto_apply_suggestions",
    ),
    # T5 redaction: a rescan of a frozen file is skipped, its first scan is not (plan §3.4).
    "tasks/redaction_task.py::redaction_detect_task": (
        "tasks/redaction_task.py::redaction_detect_task",
    ),
    "tasks/redaction_task.py::redaction_reindex_all_task": (
        "tasks/redaction_task.py::redaction_reindex_all_task",
    ),
    # T7 a manual speaker re-detection replaces speakers.
    "tasks/rediarize_task.py::rediarize_task": ("tasks/rediarize_task.py::rediarize_task",),
    # T8 writes dispatched after the initial run: speaker attributes and cross-file matching.
    "tasks/speaker_attribute_task.py::detect_speaker_attributes_task": (
        "tasks/speaker_attribute_task.py::detect_speaker_attributes_task",
    ),
    "tasks/speaker_attribute_migration_task.py::migrate_speaker_attributes_task": (
        "tasks/speaker_attribute_migration_task.py::_orchestrate_attributes",
        "tasks/speaker_attribute_migration_task.py::_reset_all_speaker_attributes",
    ),
    "tasks/speaker_attribute_migration_task.py::detect_speaker_attributes_batch_task": (
        "tasks/speaker_attribute_migration_task.py::_gender_result_writer",
        "tasks/hold_skip.py::file_is_frozen",
    ),
    "tasks/combined_speaker_analysis_task.py::analyze_speakers_combined_batch_task": (
        "tasks/combined_speaker_analysis_task.py::_combined_result_writer",
        "tasks/speaker_attribute_migration_task.py::_gender_result_writer",
    ),
    "tasks/speaker_update_task.py::process_speaker_update_background": (
        "tasks/speaker_update_task.py::_run_matching_step",
        "tasks/speaker_update_task.py::_rescore_against_profile",
        "api/endpoints/speaker_update.py::trigger_retroactive_matching",
        "api/endpoints/speaker_update.py::_load_candidates",
        "services/speaker_label_queries.py::candidate_speaker_rows",
    ),
    # Recovered titles never overwrite a held file's.
    "tasks/recovery_tasks.py::youtube_metadata_backfill": (
        "tasks/recovery_tasks.py::youtube_metadata_backfill",
    ),
    "tasks/recovery_tasks.py::media_duration_backfill": (
        "tasks/recovery_tasks.py::media_duration_backfill",
        "tasks/recovery_tasks.py::_read_duration_candidates",
    ),
}

_T_DERIVED = (
    "rebuilds a derived copy (search index, embeddings, caches, analytics, thumbnails, waveform, "
    "facts/digest, cluster grouping): allowed on a held file (plan §3.1)"
)
_T_FINISH = (
    "finishes the derived side (voiceprints, search documents, caches) of a write the API "
    "already accepted; reached only after the route's freeze guard passed, and skipping it would "
    "leave the derived copies disagreeing with the preserved record"
)
_T_INITIAL = (
    "part of a file's initial processing run (status pending/processing): not frozen by "
    "definition (plan §3.1); a re-run is refused at its entry point"
)
_T_DESTROY = "a destroy sweep that never selects a held file (retention, trash, erasure)"
_T_EXPORT = "prepares an export or download: reading a held file stays allowed"
_T_SYSTEM = "system bookkeeping with no file's content (audit, sessions, backups, settings)"
_T_INGEST = "imports new files: there is no existing (held) file to change"
_T_STATUS = (
    "status bookkeeping of stuck or lost tasks; a re-run it triggers goes through "
    "reset_file_for_retry, which refuses a held file"
)

TASK_EXEMPT: dict[str, str] = {
    **{
        k: _T_DERIVED
        for k in (
            "tasks/analytics.py::analyze_transcript_task",
            "tasks/embedding_consistency_repair.py::speaker_embedding_consistency_repair_batch_task",
            "tasks/embedding_migration_v4.py::check_migration_status_task",
            "tasks/embedding_migration_v4.py::migrate_speaker_embeddings_v4_task",
            "tasks/embedding_migration_v4.py::extract_v4_embeddings_batch_task",
            "tasks/embedding_migration_v4.py::extract_v4_embeddings_task",
            "tasks/embedding_migration_v4_finalize.py::finalize_v4_migration_task",
            "tasks/combined_speaker_analysis_task.py::migrate_speakers_combined_task",
            "tasks/imohash_recompute.py::recompute_all",
            "tasks/ingest_artifacts_task.py::generate_file_facts_task",
            "tasks/opensearch_integrity_task.py::opensearch_orphan_cleanup_task",
            "tasks/playback_rendition.py::create_playback_rendition_task",
            "tasks/privacy_label_resync_task.py::privacy_label_resync_task",
            "tasks/reindex_task.py::reindex_transcripts_task",
            "tasks/reindex_task.py::reindex_batch_task",
            "tasks/rename_digest_task.py::regenerate_rename_digests",
            "tasks/rename_propagation_task.py::propagate_speaker_rename",
            "tasks/rename_propagation_task.py::propagate_title_rename",
            "tasks/search_availability_task.py::availability_reconcile",
            "tasks/org_content_transfer_task.py::restamp",
            "tasks/search_file_field_tasks.py::update_file_access_index",
            "tasks/search_file_field_tasks.py::update_file_tags_index",
            "tasks/search_file_field_tasks.py::index_file_summary",
            "tasks/search_index_sweep_task.py::search_index_sweep_task",
            "tasks/search_indexing_task.py::index_transcript_search_task",
            "tasks/search_indexing_task.py::backfill_speaker_id_fields_task",
            "tasks/search_maintenance_task.py::search_index_maintenance_task",
            "tasks/search_maintenance_task.py::neural_search_bootstrap_task",
            "tasks/speaker_profile_sync_task.py::speaker_profile_opensearch_sync_task",
            "tasks/search_reembed_task.py::reembed_degraded_files_task",
            "tasks/speaker_clustering.py::cluster_speakers_for_file",
            "tasks/speaker_clustering.py::recluster_all_speakers",
            "tasks/speaker_embedding_consistency.py::speaker_embedding_consistency_check_task",
            "tasks/speaker_embedding_migration.py::normalize_speaker_embeddings_task",
            "tasks/thumbnail.py::generate_thumbnail_task",
            "tasks/thumbnail_migration.py::migrate_thumbnails_to_webp",
            "tasks/thumbnail_migration.py::backfill_missing_thumbnails",
            "tasks/waveform.py::generate_waveform_task",
            "tasks/waveform_generation.py::generate_waveform_data_task",
        )
    },
    "tasks/speaker_merge_task.py::process_speaker_merge_background": _T_FINISH,
    "tasks/speaker_embedding_reassignment_task.py::update_speaker_embedding_on_reassignment": (
        _T_FINISH
    ),
    **{
        k: _T_INITIAL
        for k in (
            "tasks/speaker_embedding_task.py::extract_speaker_embeddings_task",
            "tasks/transcription/cancellation.py::reconcile_cancellation",
            "tasks/transcription/core.py::transcribe_gpu_task",
            "tasks/transcription/cpu_task.py::transcribe_cpu_task",
            "tasks/transcription/diarize_task.py::diarize_gpu_task",
            "tasks/transcription/enrich.py::enrich_and_dispatch",
            "tasks/transcription/pipeline_error.py::on_pipeline_error",
            "tasks/transcription/postprocess.py::finalize_transcription",
            "tasks/transcription/postprocess.py::flush_pipeline_timing_tail",
            "tasks/transcription/preprocess.py::preprocess_for_transcription",
        )
    },
    **{
        k: _T_DESTROY
        for k in (
            "tasks/cleanup.py::cleanup_expired_files",
            "tasks/erasure_reconciliation.py::erasure_reconciliation_sweep",
            "tasks/erasure_reconciliation.py::erasure_execute_due",
            "tasks/erasure_receipt_reverify.py::erasure_receipt_reverify",
            "tasks/privacy_purge_task.py::privacy_purge_task",
            "tasks/privacy_purge_task.py::privacy_purge_reverify_task",
            "tasks/trash.py::purge_trash",
            "tasks/trash.py::purge_trash_now",
            "tasks/voiceprint_retention.py::voiceprint_retention_sweep",
            "tasks/storage_orphan_sweep_task.py::storage_orphan_sweep_task",
        )
    },
    **{
        k: _T_EXPORT
        for k in (
            "tasks/media_download.py::prepare_media_download_task",
            "tasks/media_download.py::prepare_bulk_subtitles_task",
            "tasks/transcript_document.py::prepare_transcript_document_task",
            "tasks/baseline_export.py::export_baseline_task",
            "tasks/baseline_export.py::compare_baseline_task",
        )
    },
    **{
        k: _T_SYSTEM
        for k in (
            "tasks/account_lifecycle.py::run_account_inactivity_sweep",
            "tasks/audit_outbox_relay.py::audit_outbox_relay",
            "tasks/audit_retention.py::audit_retention_sweep",
            "tasks/backup_tasks.py::check_backup_schedule",
            "tasks/backup_tasks.py::run_backup",
            "tasks/backup_tasks.py::check_mirror_schedule",
            "tasks/backup_tasks.py::run_media_mirror",
            "tasks/chat_retention.py::chat_retention_sweep",
            "tasks/cleanup.py::run_periodic_cleanup",
            "tasks/cleanup.py::run_deep_cleanup",
            "tasks/cleanup.py::system_health_check",
            "tasks/cleanup.py::orphan_upload_sweeper",
            "tasks/cleanup.py::scratch_janitor",
            "tasks/directory_sync_task.py::check_directory_sync_schedule",
            "tasks/directory_sync_task.py::run_directory_sync",
            "tasks/legal_retention.py::legal_acceptance_retention_sweep",
            "tasks/local_model_download.py::download_local_model_task",
            "tasks/session_cap.py::run_session_cap_sweep_task",
            "tasks/token_cleanup.py::cleanup_expired_refresh_tokens_task",
            "tasks/telemetry.py::usage_report_task",
            "tasks/tenant_backfill_task.py::backfill_organization_id_task",
            "tasks/utility.py::update_gpu_stats",
            "tasks/recovery_tasks.py::youtube_metadata_fetch",
            "tasks/watch_source_tasks.py::send_notification",
            "tasks/watch_source_tasks.py::cleanup_temp",
            "tasks/auth_mail_tasks.py::send_password_reset_mail",
            "tasks/auth_mail_tasks.py::send_email_verification_mail",
            "tasks/auth_mail_tasks.py::send_invitation_mail",
            "tasks/auth_mail_tasks.py::send_security_notice_mail",
        )
    },
    **{
        k: _T_INGEST
        for k in (
            "tasks/media_playlist_processing.py::process_youtube_playlist_task",
            "tasks/youtube_processing.py::process_youtube_url_task",
            "tasks/watch_source_tasks.py::scan_all",
            "tasks/watch_source_tasks.py::scan_single",
            "tasks/watch_source_tasks.py::stitch_and_import",
        )
    },
    **{
        k: _T_STATUS
        for k in (
            "tasks/cleanup.py::emergency_file_recovery",
            "tasks/recovery.py::startup_recovery_task",
            "tasks/recovery.py::recover_user_files_task",
            "tasks/recovery.py::reclaim_lost_tasks_task",
            "tasks/recovery.py::reclaim_orphaned_deliveries_task",
            "tasks/recovery.py::periodic_health_check_task",
        )
    },
}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_every_write_route_is_classified_and_guarded():
    index = _app_index()
    assert len(index.routes) > 300, f"the route scan found only {len(index.routes)} routes"
    problems = classify(
        index.routes,
        index,
        guarded=ROUTE_FREEZE_GUARDED,
        exempt=ROUTE_NOT_FILE_CONTENT,
        helper_guarded=ROUTE_DESTROY_GUARDED,
        annotated=ROUTE_ALLOWED_ON_HELD,
    )
    assert not problems, "\n".join(problems)


def test_every_celery_task_is_classified_and_guarded():
    index = _app_index()
    assert len(index.tasks) > 100, f"the task scan found only {len(index.tasks)} tasks"
    problems = classify(index.tasks, index, guarded=TASK_FREEZE_GUARDED, exempt=TASK_EXEMPT)
    assert not problems, "\n".join(problems)


def test_each_guard_wrapper_reaches_a_hold_guard_primitive():
    index = _app_index()
    for name, key in GUARD_WRAPPERS.items():
        assert _fn(key) == name, key
        assert key in index.calls, f"{key} does not exist"
        reached = index.calls[key] & (GUARD_PRIMITIVES | frozenset(GUARD_WRAPPERS))
        assert reached, f"{key} no longer calls a hold_guard primitive"


def test_the_freeze_action_vocabulary_is_pinned():
    """``content.hold.refused`` actions are a closed set; the first four predate the freeze."""
    from app.services.hold_guard import FREEZE_ACTIONS

    assert FREEZE_ACTIONS[:4] == ("delete", "cancel_upload", "summary_delete", "speaker_delete")
    assert len(set(FREEZE_ACTIONS)) == len(FREEZE_ACTIONS)
    assert set(FREEZE_ACTIONS) >= {
        "segment_edit",
        "segment_speaker",
        "speaker_merge",
        "speaker_create",
        "speaker_update",
        "speaker_link",
        "speaker_verify",
        "speaker_gender",
        "file_update",
        "summary_generate",
        "identify_speakers",
        "topic_extract",
        "topic_apply",
        "topic_dismiss",
        "auto_label",
        "redaction_rescan",
        "tag_add",
        "tag_remove",
        "tag_merge",
        "tag_delete",
        "collection_add",
        "remove_from_collection",
        "delete_collection",
        "comment_edit",
        "comment_delete",
        "reprocess",
        "retry",
        "recover",
        "profile_delete",
    }


def test_an_unknown_refusal_action_is_a_programming_error():
    from app.api.hold_refusal import refuse_if_frozen

    with pytest.raises(ValueError, match="FREEZE_ACTIONS"):
        # The session is never touched: the unknown action is rejected before any lookup.
        refuse_if_frozen(cast("Session", None), [], actor=None, action="not_an_action")


# ---------------------------------------------------------------------------
# Self-test: the scanner must fire, and must stay clean
# ---------------------------------------------------------------------------

_SYNTHETIC = {
    "api/endpoints/fake.py": (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.put('/{uuid}')\n"
        "def edit_unguarded(uuid):\n"
        "    return write(uuid)\n"
        "@router.put('/{uuid}/guarded')\n"
        "def edit_guarded(db, uuid):\n"
        "    refuse_if_frozen(db, 1, actor=None, action='file_update')\n"
        "@router.post('/{uuid}/via-helper')\n"
        "def edit_via_helper(db, uuid):\n"
        "    return helper(db, uuid)\n"
        "def helper(db, uuid):\n"
        "    return inner(db, uuid)\n"
        "def inner(db, uuid):\n"
        "    assert_content_mutable(db, [uuid])\n"
        "@router.get('/{uuid}')\n"
        "def a_read(uuid):\n"
        "    return uuid\n"
    ),
}
_GUARDED = "api/endpoints/fake.py::edit_guarded"
_UNGUARDED = "api/endpoints/fake.py::edit_unguarded"
_VIA = "api/endpoints/fake.py::edit_via_helper"
_HELPER = "api/endpoints/fake.py::helper"
_INNER = "api/endpoints/fake.py::inner"


def test_selftest_an_unguarded_route_is_reported():
    index = build_index(_SYNTHETIC)
    assert index.routes == {_GUARDED, _UNGUARDED, _VIA}, "a GET must not be scanned"
    problems = classify(
        index.routes, index, guarded={_GUARDED: (_GUARDED,), _VIA: (_HELPER, _INNER)}, exempt={}
    )
    assert problems == [f"{_UNGUARDED}: unclassified; {_HINT}"]


def test_selftest_a_route_classified_guarded_that_calls_no_guard_is_reported():
    index = build_index(_SYNTHETIC)
    problems = classify(
        index.routes,
        index,
        guarded={_GUARDED: (_GUARDED,), _UNGUARDED: (_UNGUARDED,), _VIA: (_HELPER, _INNER)},
        exempt={},
    )
    assert len(problems) == 1 and "calls no hold guard" in problems[0], problems


def test_selftest_a_broken_chain_is_reported():
    """The helper exists and guards, but the route does not reach it through ``helper``."""
    index = build_index(_SYNTHETIC)
    problems = classify(
        index.routes,
        index,
        guarded={_GUARDED: (_GUARDED,), _VIA: (_INNER,)},
        exempt={_UNGUARDED: "a reason"},
    )
    assert problems == [f"{_VIA}: {_VIA} no longer calls inner"], problems


def test_selftest_a_stale_entry_is_reported():
    index = build_index(_SYNTHETIC)
    gone = "api/endpoints/fake.py::removed_route"
    problems = classify(
        index.routes,
        index,
        guarded={_GUARDED: (_GUARDED,), _VIA: (_HELPER, _INNER)},
        exempt={_UNGUARDED: "a reason", gone: "a reason"},
    )
    assert problems == [f"{gone}: stale exempt entry, no such write path"], problems


def test_selftest_a_clean_tree_is_clean():
    index = build_index(_SYNTHETIC)
    problems = classify(
        index.routes,
        index,
        guarded={_GUARDED: (_GUARDED,), _VIA: (_HELPER, _INNER)},
        exempt={_UNGUARDED: "a reason"},
    )
    assert problems == []


def test_selftest_a_task_is_found_and_a_dispatch_counts_as_a_call():
    source = {
        "tasks/fake.py": (
            "@celery_app.task(name='x')\n"
            "def outer():\n"
            "    inner_task.delay(1)\n"
            "@celery_app.task(name='y')\n"
            "def inner_task(file_id):\n"
            "    if file_is_frozen(file_id=file_id):\n"
            "        return None\n"
        )
    }
    index = build_index(source)
    assert index.tasks == {"tasks/fake.py::outer", "tasks/fake.py::inner_task"}
    problems = classify(
        index.tasks,
        index,
        guarded={
            "tasks/fake.py::outer": ("tasks/fake.py::inner_task",),
            "tasks/fake.py::inner_task": ("tasks/fake.py::inner_task",),
        },
        exempt={},
    )
    assert problems == []
