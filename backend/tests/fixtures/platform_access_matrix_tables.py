"""Hand-written classification tables for the platform-access matrix (issue #1281).

``platform_access_matrix.py`` derives every row it can from the route table and an AST scan; the
judgements a machine cannot make live here, each keyed so that a NEW route or site has no entry
and shows up as ``UNCLASSIFIED`` until a human decides.
"""

from __future__ import annotations

#: How a content route (one that takes the request context) keeps to the caller's reach. A route
#: is listed here by "METHOD path"; a content route that is not listed is ``UNCLASSIFIED``.
#: ``bypass``: the handler itself consults the request bypass (checked against its code by
#: ``test_platform_access_matrix.py``, so a docstring or comment cannot stand in for the call);
#: ``bypass-scope`` is the same check for a route whose bypass question is the deployment scope
#: (the shared tag vocabulary) rather than a loaded row.
#: ``caller``: the handler forwards the context or filters on the caller; the leak sweep in
#: ``tests/api/test_support_access_matrix.py`` is the evidence for those.
CONTENT_GUARDS: dict[str, str] = {
    "DELETE /api/collections/{collection_uuid}": "caller",
    "DELETE /api/collections/{collection_uuid}/media": "caller",
    "DELETE /api/comments/{comment_uuid}": "bypass",
    "DELETE /api/custom-vocabulary/all": "caller",
    "DELETE /api/custom-vocabulary/{term_id}": "caller",
    "DELETE /api/files/{file_uuid}": "bypass",
    "DELETE /api/files/{file_uuid}/cache": "bypass",
    "DELETE /api/files/{file_uuid}/force": "bypass",
    "DELETE /api/files/{file_uuid}/suggestions": "bypass",
    "DELETE /api/files/{file_uuid}/summary": "bypass",
    "DELETE /api/groups/{group_uuid}": "caller",
    "DELETE /api/groups/{group_uuid}/members/{user_uuid}": "caller",
    "DELETE /api/speaker-clusters/{cluster_uuid}": "caller",
    "DELETE /api/speaker-profiles/profiles/{profile_uuid}": "caller",
    "DELETE /api/speaker-profiles/profiles/{profile_uuid}/avatar": "caller",
    "DELETE /api/speakers/{speaker_uuid}": "bypass",
    "DELETE /api/tags": "bypass-scope",
    "DELETE /api/tags/cleanup": "bypass-scope",
    "DELETE /api/tags/files/{file_uuid}/tags/{tag_name}": "bypass",
    "DELETE /api/watch-sources/{source_uuid}": "caller",
    "DELETE /api/watch-sources/{source_uuid}/emails/{config_uuid}": "caller",
    "DELETE /api/watch-sources/{source_uuid}/files/{file_uuid}": "caller",
    "GET /api/asr-settings": "caller",
    "GET /api/asr-settings/config/{config_uuid}": "caller",
    "GET /api/asr-settings/status": "caller",
    "GET /api/collections": "bypass",
    "GET /api/collections/for-files": "caller",
    "GET /api/collections/shared-with-me": "bypass",
    "GET /api/collections/{collection_uuid}": "bypass",
    "GET /api/collections/{collection_uuid}/media": "bypass",
    "GET /api/collections/{collection_uuid}/shares": "caller",
    "GET /api/comments": "bypass",
    "GET /api/comments/files/{file_uuid}/comments": "bypass",
    "GET /api/comments/{comment_uuid}": "caller",
    "GET /api/custom-vocabulary": "caller",
    "GET /api/custom-vocabulary/export": "caller",
    "GET /api/files": "bypass",
    "GET /api/files/management/stuck": "bypass",
    "GET /api/files/metadata-filters": "bypass",
    "GET /api/files/owners": "bypass",
    "GET /api/files/{file_uuid}": "bypass",
    "GET /api/files/{file_uuid}/analytics": "bypass",
    "GET /api/files/{file_uuid}/captions": "bypass",
    "GET /api/files/{file_uuid}/info": "bypass",
    "GET /api/files/{file_uuid}/post-processing": "bypass",
    "GET /api/files/{file_uuid}/segments": "bypass",
    "GET /api/files/{file_uuid}/status-detail": "bypass",
    "GET /api/files/{file_uuid}/stream-url": "bypass",
    "GET /api/files/{file_uuid}/subtitles/validate": "bypass",
    "GET /api/files/{file_uuid}/suggestions": "bypass",
    "GET /api/files/{file_uuid}/summary": "bypass",
    "GET /api/files/{file_uuid}/summary-status": "bypass",
    "GET /api/files/{file_uuid}/waveform": "bypass",
    "GET /api/files/{file_uuid}/waveform/peaks": "bypass",
    "GET /api/groups": "caller",
    "GET /api/groups/{group_uuid}": "caller",
    "GET /api/llm-settings": "caller",
    "GET /api/llm-settings/config/{config_uuid}": "caller",
    "GET /api/llm-settings/config/{config_uuid}/context-window": "caller",
    "GET /api/llm-settings/config/{config_uuid}/reasoning": "caller",
    "GET /api/llm-settings/status": "caller",
    "GET /api/my-files/status": "bypass",
    "GET /api/my-files/{file_uuid}/status": "bypass",
    "GET /api/prompts": "caller",
    "GET /api/prompts/active/current": "caller",
    "GET /api/prompts/by-content-type/{content_type}": "caller",
    "GET /api/prompts/shared/library": "caller",
    "GET /api/prompts/{prompt_uuid}": "caller",
    "GET /api/speaker-clusters": "caller",
    "GET /api/speaker-clusters/speakers/{speaker_uuid}/media-preview": "bypass",
    "GET /api/speaker-clusters/stats": "caller",
    "GET /api/speaker-clusters/unverified/inbox": "bypass",
    "GET /api/speaker-clusters/{cluster_uuid}": "caller",
    "GET /api/speaker-profiles/collections": "caller",
    "GET /api/speaker-profiles/profiles": "bypass",
    "GET /api/speaker-profiles/profiles/{profile_uuid}/occurrences": "bypass",
    "GET /api/speaker-profiles/speakers/{speaker_uuid}/suggestions": "bypass",
    "GET /api/speakers": "bypass",
    "GET /api/speakers/{speaker_uuid}": "bypass",
    "GET /api/speakers/{speaker_uuid}/cross-media": "bypass",
    "GET /api/tags": "caller",
    "GET /api/tags/collisions": "caller",
    "GET /api/tags/for-files": "caller",
    "GET /api/tags/impact": "bypass-scope",
    "GET /api/tags/unused": "caller",
    "GET /api/tags/{tag_uuid}/files": "bypass",
    "GET /api/tags/{tag_uuid}/shares": "bypass-scope",
    "GET /api/tasks": "bypass",
    "GET /api/tasks/progress/active": "bypass",
    "GET /api/tasks/{task_id}": "bypass",
    "GET /api/usage/me": "caller",
    "GET /api/usage/me/daily": "caller",
    "GET /api/user-settings/media-sources": "caller",
    "GET /api/user-settings/organization-context/shared": "caller",
    "GET /api/users/search": "caller",
    "GET /api/watch-sources": "bypass",
    "GET /api/watch-sources/{source_uuid}": "caller",
    "GET /api/watch-sources/{source_uuid}/emails": "caller",
    "GET /api/watch-sources/{source_uuid}/emails/available": "caller",
    "GET /api/watch-sources/{source_uuid}/files": "caller",
    "GET /api/watch-sources/{source_uuid}/files/stats": "caller",
    "PATCH /api/tags/{tag_uuid}": "bypass-scope",
    "POST /api/asr-settings/set-active": "caller",
    "POST /api/asr-settings/test-config/{config_uuid}": "caller",
    "POST /api/collections/{collection_uuid}/media": "caller",
    "POST /api/comments": "bypass",
    "POST /api/comments/files/{file_uuid}/comments": "bypass",
    "POST /api/custom-vocabulary": "caller",
    "POST /api/custom-vocabulary/bulk": "caller",
    "POST /api/files/batch-extract": "bypass",
    "POST /api/files/management/bulk-action": "bypass",
    "POST /api/files/retroactive-auto-label": "caller",
    "POST /api/files/{file_uuid}/analytics/refresh": "bypass",
    "POST /api/files/{file_uuid}/apply": "bypass",
    "POST /api/files/{file_uuid}/auto-label": "bypass",
    "POST /api/files/{file_uuid}/cancel": "bypass",
    "POST /api/files/{file_uuid}/extract": "bypass",
    "POST /api/files/{file_uuid}/identify-speakers": "bypass",
    "POST /api/files/{file_uuid}/recover": "bypass",
    "POST /api/files/{file_uuid}/reprocess": "bypass",
    "POST /api/files/{file_uuid}/retry": "bypass",
    "POST /api/files/{file_uuid}/retry-summary": "bypass",
    "POST /api/files/{file_uuid}/summarize": "bypass",
    "POST /api/files/{file_uuid}/waveform/generate": "bypass",
    "POST /api/groups": "caller",
    "POST /api/groups/{group_uuid}/members": "caller",
    "POST /api/llm-settings/config/{config_uuid}/context-window-probe": "caller",
    "POST /api/llm-settings/config/{config_uuid}/reasoning-probe": "caller",
    "POST /api/llm-settings/set-active": "caller",
    "POST /api/llm-settings/test-config/{config_uuid}": "caller",
    "POST /api/llm-settings/test-current": "caller",
    "POST /api/my-files/request-recovery": "caller",
    "POST /api/my-files/{file_uuid}/retry": "bypass",
    "POST /api/prompts/active/set": "caller",
    "POST /api/prompts/shared/{prompt_uuid}/toggle": "caller",
    "POST /api/prompts/{prompt_uuid}/clone": "caller",
    "POST /api/speaker-clusters/batch-verify": "caller",
    "POST /api/speaker-clusters/{cluster_uuid}/analyze-outliers": "caller",
    "POST /api/speaker-clusters/{cluster_uuid}/promote": "caller",
    "POST /api/speaker-clusters/{cluster_uuid}/split": "caller",
    "POST /api/speaker-clusters/{cluster_uuid}/unassign": "caller",
    "POST /api/speaker-clusters/{source_uuid}/merge/{target_uuid}": "caller",
    "POST /api/speaker-profiles/profiles/{profile_uuid}/avatar": "caller",
    "POST /api/speaker-profiles/profiles/{profile_uuid}/confirm-gender": "caller",
    "POST /api/speaker-profiles/speakers/{speaker_uuid}/assign-profile": "bypass",
    "POST /api/speakers": "bypass",
    "POST /api/speakers/{speaker_uuid}/confirm-gender": "bypass",
    "POST /api/speakers/{speaker_uuid}/merge/{target_speaker_uuid}": "bypass",
    "POST /api/speakers/{speaker_uuid}/verify": "bypass",
    "POST /api/tags/files/{file_uuid}/tags": "bypass",
    "POST /api/tags/promote": "bypass-scope",
    "POST /api/tags/{tag_uuid}/merge": "bypass-scope",
    "POST /api/tasks/retry/{file_uuid}": "bypass",
    "POST /api/user-settings/organization-context/use-shared": "caller",
    "POST /api/watch-sources/{source_uuid}/emails": "caller",
    "POST /api/watch-sources/{source_uuid}/files/bulk-delete": "caller",
    "POST /api/watch-sources/{source_uuid}/files/retry": "caller",
    "POST /api/watch-sources/{source_uuid}/scan": "caller",
    "POST /api/watch-sources/{source_uuid}/test": "caller",
    "PUT /api/collections/{collection_uuid}": "caller",
    "PUT /api/comments/{comment_uuid}": "bypass",
    "PUT /api/custom-vocabulary/{term_id}": "caller",
    "PUT /api/files/{file_uuid}": "bypass",
    "PUT /api/files/{file_uuid}/transcript/segments/{segment_uuid}": "bypass",
    "PUT /api/groups/{group_uuid}": "caller",
    "PUT /api/groups/{group_uuid}/members/{user_uuid}": "caller",
    "PUT /api/speaker-clusters/{cluster_uuid}": "caller",
    "PUT /api/speaker-profiles/profiles/{profile_uuid}": "bypass",
    "PUT /api/speakers/{speaker_uuid}": "bypass",
    "PUT /api/transcripts/segments/{segment_uuid}/speaker": "bypass",
    "PUT /api/watch-sources/{source_uuid}": "caller",
}

CONTENT_GUARD_TEXT = {
    "bypass": "bypass.allows on the loaded row, or sees_all_in_scope inside the tenant",
    "bypass-scope": (
        "tenant-owned tags are the caller's own inside the request tenant; the shared vocabulary "
        "and deployment-wide sweeps are decided by ctx.bypass.deployment_scope_writable "
        "(any admin in a single-tenant deployment, a super_admin otherwise, never a grant)"
    ),
    "caller": "caller-scoped through the request context (ctx forwarded or user filter)",
}

#: Routes whose handler tests the caller's role inline, whatever else gates them. The dependency
#: walk cannot see such a test, so it is found by reading the handler's code (and the helpers it
#: hands the user to) for a role read on the signed-in user; a route that does so and is not listed
#: here is ``UNCLASSIFIED``. Every route is scanned, including ones that take the request context:
#: the context does not make an inline role test "caller-scoped".
#: ``"METHOD path" -> (category, why that category is acceptable)``.
INLINE_ROLE_ROUTES: dict[str, tuple[str, str]] = {
    "POST /api/files/waveforms/generate": (
        "state-repair",
        "queues waveform generation for files that lack it and returns counts and a task id; "
        "never returns file content",
    ),
    "GET /api/files/waveforms/status": (
        "platform-ops",
        "deployment-wide waveform coverage counts; no filenames or content",
    ),
    "POST /api/files/management/cleanup-orphaned": (
        "state-repair",
        "recovers files stuck in processing and returns counts and ids' outcome; never returns "
        "file content",
    ),
    "GET /api/asr-settings/providers": (
        "settings",
        "the role only decides which cloud providers the catalog lists; no tenant content",
    ),
    "GET /api/files/{file_uuid}/thumbnail": (
        "content-optional-auth",
        "optional-auth route; the role only decides takedown-review visibility, and the file is "
        "decided by the chokepoint (see this handler's allows and grant-resolver site rows)",
    ),
    "POST /api/legal/acknowledgments": (
        "settings",
        "only a super_admin may accept the operator acknowledgment; deployment-level record",
    ),
    "DELETE /api/admin/users/{user_uuid}": (
        "account-admin",
        "the role test only narrows the admin gate: an admin may not act on a super_admin; no tenant content is read or returned",
    ),
    "DELETE /api/admin/users/{user_uuid}/sessions": (
        "account-admin",
        "the role test only narrows the admin gate: an admin may not revoke a super_admin's sessions; no tenant content",
    ),
    "POST /api/admin/users/{user_uuid}/lock": (
        "account-admin",
        "the role test only narrows the admin gate: an admin may not lock a super_admin; no tenant content",
    ),
    "POST /api/admin/users": (
        "account-admin",
        "the role test only narrows the admin gate: only a super_admin may create an elevated-role account; no tenant content",
    ),
    "PUT /api/users/{user_uuid}": (
        "account-admin",
        "the role test only narrows the admin gate: only a super_admin may touch a super_admin or grant an elevated role; no tenant content",
    ),
    "DELETE /api/users/{user_uuid}": (
        "account-admin",
        "the role test only narrows the admin gate: only a super_admin may delete a super_admin; no tenant content",
    ),
    "POST /api/auth/invitations": (
        "account-admin",
        "the role test only narrows the admin gate: only a super_admin may invite into an elevated role; no tenant content",
    ),
    "GET /api/support-access/grants": (
        "grant-lifecycle",
        "the role test decides whether staff list every grant (super_admin) or only their own; grants carry ids, reasons and expiry, never tenant content",
    ),
    "GET /api/support-access/grants/{grant_uuid}": (
        "grant-lifecycle",
        "visibility of a grant record to its grantee or a super_admin (helper reads the flag); the record holds ids and expiry, never tenant content",
    ),
    "GET /api/support-access/grants/{grant_uuid}/uses": (
        "grant-lifecycle",
        "same visibility rule as the grant record; the use log names resource types and uuids, never content",
    ),
    "DELETE /api/files/{file_uuid}/force": (
        "admin-precondition",
        "the role test is the route's admin precondition; the file is then resolved through get_deletable_file with ctx.bypass, so tenant reach is decided on the row (and by the grant), not by the role",
    ),
    "GET /api/files/{file_uuid}/status-detail": (
        "display-hint",
        "the role only decides whether the force-delete action is offered in the response; the file was resolved with ctx.bypass first",
    ),
    "POST /api/files/{file_uuid}/retry": (
        "retry-ceiling",
        "admins skip the retry ceiling, evaluated after the file was resolved with ctx.bypass and editor permission",
    ),
    "POST /api/tasks/retry/{file_uuid}": (
        "retry-ceiling",
        "admins skip the retry ceiling, evaluated after the chokepoint passed with ctx.bypass and editor permission",
    ),
    "POST /api/files/management/bulk-action": (
        "retry-ceiling",
        "the role is forwarded to the per-file handlers, which skip the retry ceiling and allow force for admins; each file still passes the chokepoint with ctx.bypass",
    ),
    "GET /api/files/{file_uuid}/summary-status": (
        "display-hint",
        "the role only decides the can_generate hint in the response; the file was resolved with ctx.bypass first",
    ),
    "POST /api/files/{file_uuid}/summarize": (
        "settings-override",
        "an admin may generate a summary while the system-wide switch is off; the file was resolved with ctx.bypass and the summary is the file owner's",
    ),
    "GET /api/search/index-health": (
        "platform-ops",
        "non-admins get document counts scoped to themselves, admins the deployment totals; counts only, no content",
    ),
    "GET /api/user-settings/media-sources": (
        "display-hint",
        "a helper reads the owner's role to fill the owner_role label on the caller's own and tenant-shared sources; no access decision",
    ),
    "POST /api/user-settings/media-sources": (
        "display-hint",
        "a helper reads the owner's role to fill the owner_role label on the response; no access decision",
    ),
    "PUT /api/user-settings/media-sources/{source_uuid}": (
        "display-hint",
        "a helper reads the owner's role to fill the owner_role label on the response; no access decision",
    ),
    "POST /api/prompts/shared/{prompt_uuid}/toggle": (
        "owner-or-tenant-admin",
        "the owner, or an admin of the prompt's own tenant, may change sharing; a prompt outside the "
        "caller's tenant answers 404 via _in_tenant, and a write support grant assumes the granted tenant",
    ),
}

#: Every place a request's bypass widens a list to every user's rows (``scope-widener``) or lets
#: an admin reveal unredacted text (``reveal-widener``), keyed ``rel::function``. A widener in a
#: function that is not listed is ``UNCLASSIFIED``: a new widener is a new decision.
SCOPE_WIDENER_SITES: dict[str, str] = {
    "api/endpoints/files/__init__.py::list_media_files": (
        "widens a list to every user's rows; the gallery list drops the owner filter but keeps the organization predicate"
    ),
    "api/endpoints/files/filtering.py::get_accessible_owners": (
        "widens a list to every user's rows; the owner facet lists users of every file in the active organization scope, never beyond it"
    ),
    "api/endpoints/search.py::get_degraded_embeddings": (
        "widens a list to every user's rows; the degraded-embeddings listing keeps the "
        "organization predicate and names other tenants' files never, only counts them"
    ),
    "api/endpoints/speaker_profiles.py::list_speaker_profiles": (
        "widens a list to every user's rows; every profile of the active tenant, filtered by the organization stamp"
    ),
    "api/endpoints/speaker_profiles.py::update_speaker_profile": (
        "widens a list to every user's rows; a rename reaches other users' speakers only after the profile itself passed the tenant gate"
    ),
    "api/endpoints/speakers.py::_get_display_name_based_occurrences": (
        "widens a list to every user's rows; cross-media occurrences keep the organization predicate on the file"
    ),
    "api/endpoints/speakers.py::_get_profile_based_occurrences": (
        "widens a list to every user's rows; cross-media occurrences keep the organization predicate on the file"
    ),
    "api/endpoints/speakers.py::_get_unique_speakers_for_filter": (
        "widens a list to every user's rows; the speaker picker offers speakers of files in the active scope only"
    ),
    "api/endpoints/speakers.py::_handle_update_profile_action": (
        "widens a list to every user's rows; the profile is looked up inside the tenant before the rename is applied"
    ),
    "api/endpoints/speakers.py::_resolve_profile_filter_id": (
        "widens a list to every user's rows; a profile uuid resolves only inside the active tenant"
    ),
    "api/endpoints/speakers.py::debug_cross_media_by_name": (
        "widens a list to every user's rows; the admin diagnostic stays inside the active tenant and widens the owner filter only under the bypass"
    ),
    "api/endpoints/speakers.py::list_speakers": (
        "widens a list to every user's rows; every speaker of files in the active scope; the owner filter is the only thing dropped"
    ),
    "api/endpoints/tasks.py::_get_task_row_by_id": (
        "widens a list to every user's rows; the task row is only a pointer: the tenant gate is applied to the media file it names by the caller"
    ),
    "api/endpoints/tasks.py::_visible_in_tenant": (
        "widens a list to every user's rows; the owner half widens, the tenant stamp always applies"
    ),
    "api/endpoints/tasks.py::get_active_progress": (
        "widens a list to every user's rows; progress state is keyed by the caller's own id; the widener only skips narrowing the file list"
    ),
    "api/endpoints/watch_sources.py::list_watch_sources": (
        "widens a list to every user's rows; scope=all lists every user's sources inside the active organization only"
    ),
}
REVEAL_WIDENER_SITES: dict[str, str] = {
    "api/endpoints/files/crud.py::_resolve_redaction_for_request": (
        "owner-only reveal in multi mode, admin reveal only in single mode; detail view: the owner reveals; a non-owner admin only where the bypass allows it, which is single mode"
    ),
    "api/endpoints/files/subtitles.py::_resolve_subtitle_redaction": (
        "owner-only reveal in multi mode, admin reveal only in single mode; subtitle download: same rule as the detail view"
    ),
    "api/endpoints/files/transcript_export.py::_resolve_export_redaction": (
        "owner-only reveal in multi mode, admin reveal only in single mode; transcript export: same rule as the detail view"
    ),
}

#: For each scope widener, a name that must still appear in the same function's code: the tenant
#: predicate the widening is subject to. Removing it (leaving the widening in place) fails
#: ``test_every_scope_widener_still_applies_its_tenant_predicate``. An empty tuple means the
#: tenant gate is applied by the caller, and the site's reason says where.
TENANT_MARKERS: dict[str, tuple[str, ...]] = {
    "api/endpoints/files/__init__.py::list_media_files": ("org_pred",),
    "api/endpoints/files/filtering.py::get_accessible_owners": ("org_pred",),
    "api/endpoints/search.py::get_degraded_embeddings": ("org_scope_pred",),
    "api/endpoints/speaker_profiles.py::list_speaker_profiles": ("org_scope_pred",),
    "api/endpoints/speaker_profiles.py::update_speaker_profile": ("require_profile_in_scope",),
    "api/endpoints/speakers.py::_get_display_name_based_occurrences": ("org_scope_pred",),
    "api/endpoints/speakers.py::_get_profile_based_occurrences": ("org_scope_pred",),
    "api/endpoints/speakers.py::_get_unique_speakers_for_filter": ("file_ids_in_scope",),
    "api/endpoints/speakers.py::_handle_update_profile_action": ("org_scope_pred",),
    "api/endpoints/speakers.py::_resolve_profile_filter_id": ("org_scope_pred",),
    "api/endpoints/speakers.py::debug_cross_media_by_name": ("file_ids_in_scope",),
    "api/endpoints/speakers.py::list_speakers": ("file_ids_in_scope",),
    "api/endpoints/tasks.py::_get_task_row_by_id": (),
    "api/endpoints/tasks.py::_visible_in_tenant": ("org_scope_pred",),
    "api/endpoints/tasks.py::get_active_progress": ("get_active_tasks",),
    "api/endpoints/watch_sources.py::list_watch_sources": ("org_filter",),
}
