"""Every route the load test calls, which persona covers each API group, and what each needs.

Locust-free, like ``scenario.py``. ``backend/tests/unit/test_load_scenario_routes.py`` checks:

- every ``ROUTES`` entry is a real ``(method, path)`` in ``backend/openapi.json`` and is called;
- every OpenAPI tag group is either covered (``GROUP_PERSONAS``, exactly the personas whose
  tasks reach it) or listed in ``EXCLUDED_GROUPS`` with the reason;
- ``SEED_NEEDS`` holds on a small seed made with the README's load-test flags.
"""

from __future__ import annotations

# (method, OpenAPI path template). Every HTTP request the scenario makes goes through one of these.
ROUTES: dict[str, tuple[str, str]] = {
    # sign-in and page boot
    'login': ('POST', '/api/auth/login'),
    'session': ('GET', '/api/auth/session'),
    'me': ('GET', '/api/auth/me'),
    'system.capabilities': ('GET', '/api/system/capabilities'),
    # gallery
    'files.list': ('GET', '/api/files'),
    'files.metadata_filters': ('GET', '/api/files/metadata-filters'),
    'files.owners': ('GET', '/api/files/owners'),
    'files.trash': ('GET', '/api/files/trash'),
    'tags.list': ('GET', '/api/tags'),
    'collections.list': ('GET', '/api/collections'),
    'collections.shared_with_me': ('GET', '/api/collections/shared-with-me'),
    'collections.media': ('GET', '/api/collections/{collection_uuid}/media'),
    # a file's page
    'files.detail': ('GET', '/api/files/{file_uuid}'),
    'files.segments': ('GET', '/api/files/{file_uuid}/segments'),
    'files.analytics': ('GET', '/api/files/{file_uuid}/analytics'),
    'files.summary_status': ('GET', '/api/files/{file_uuid}/summary-status'),
    'files.post_processing': ('GET', '/api/files/{file_uuid}/post-processing'),
    'summary.get': ('GET', '/api/files/{file_uuid}/summary'),
    'topics.suggestions': ('GET', '/api/files/{file_uuid}/suggestions'),
    'comments.list': ('GET', '/api/comments/files/{file_uuid}/comments'),
    'speakers.list': ('GET', '/api/speakers'),
    'tags.for_files': ('GET', '/api/tags/for-files'),
    'collections.for_files': ('GET', '/api/collections/for-files'),
    'files.stream_url': ('GET', '/api/files/{file_uuid}/stream-url'),
    'files.waveform': ('GET', '/api/files/{file_uuid}/waveform'),
    'files.subtitles': ('GET', '/api/files/{file_uuid}/subtitles'),
    'files.export': ('GET', '/api/files/{file_uuid}/export'),
    'files.captions': ('GET', '/api/files/{file_uuid}/captions'),
    # search, speakers, tasks
    'search': ('GET', '/api/search'),
    'search.suggestions': ('GET', '/api/search/suggestions'),
    'search.count': ('GET', '/api/search/count'),
    'profiles.list': ('GET', '/api/speaker-profiles/profiles'),
    'profiles.occurrences': ('GET', '/api/speaker-profiles/profiles/{profile_uuid}/occurrences'),
    'clusters.list': ('GET', '/api/speaker-clusters'),
    'clusters.stats': ('GET', '/api/speaker-clusters/stats'),
    'clusters.inbox': ('GET', '/api/speaker-clusters/unverified/inbox'),
    'speakers.suggestions': ('GET', '/api/speaker-profiles/speakers/{speaker_uuid}/suggestions'),
    'speakers.history': ('GET', '/api/speakers/{speaker_uuid}/identity-history'),
    'speakers.cross_media': ('GET', '/api/speakers/{speaker_uuid}/cross-media'),
    'collections.shares': ('GET', '/api/collections/{collection_uuid}/shares'),
    'summary.export': ('GET', '/api/files/{file_uuid}/summary/export'),
    'tasks.list': ('GET', '/api/tasks'),
    'tasks.active': ('GET', '/api/tasks/progress/active'),
    'my_files.status': ('GET', '/api/my-files/status'),
    # settings pages (reads only)
    'settings.all': ('GET', '/api/user-settings/all'),
    'settings.chat': ('GET', '/api/user-settings/chat'),
    'settings.redaction': ('GET', '/api/user-settings/redaction'),
    'settings.diarization': ('GET', '/api/user-settings/diarization/pyannote'),
    'settings.pipeline': ('GET', '/api/user-settings/pipeline'),
    'settings.asr': ('GET', '/api/asr-settings'),
    'settings.llm': ('GET', '/api/llm-settings'),
    'vocabulary.list': ('GET', '/api/custom-vocabulary'),
    'prompts.list': ('GET', '/api/prompts'),
    'privacy.me': ('GET', '/api/privacy/me'),
    'legal.documents': ('GET', '/api/legal/documents'),
    'auth.sessions': ('GET', '/api/auth/sessions'),
    'usage.me': ('GET', '/api/usage/me'),
    'groups.list': ('GET', '/api/groups'),
    'users.me': ('GET', '/api/users/me'),
    'version': ('GET', '/api/version'),
    # editor writes (each cleaned up in the same task)
    'comments.create': ('POST', '/api/comments/files/{file_uuid}/comments'),
    'comments.update': ('PUT', '/api/comments/{comment_uuid}'),
    'comments.delete': ('DELETE', '/api/comments/{comment_uuid}'),
    'tags.add_to_file': ('POST', '/api/tags/files/{file_uuid}/tags'),
    'tags.delete': ('DELETE', '/api/tags'),
    'collections.create': ('POST', '/api/collections'),
    'collections.add_media': ('POST', '/api/collections/{collection_uuid}/media'),
    'collections.remove_media': ('DELETE', '/api/collections/{collection_uuid}/media'),
    'collections.delete': ('DELETE', '/api/collections/{collection_uuid}'),
    'segments.update': ('PUT', '/api/files/{file_uuid}/transcript/segments/{segment_uuid}'),
    'speakers.create': ('POST', '/api/speakers'),
    'speakers.update': ('PUT', '/api/speakers/{speaker_uuid}'),
    'speakers.merge': ('POST', '/api/speakers/{speaker_uuid}/merge/{target_speaker_uuid}'),
    'segments.speaker': ('PUT', '/api/transcripts/segments/{segment_uuid}/speaker'),
    'files.hide': ('PUT', '/api/files/{file_uuid}/hidden'),
    'files.unhide': ('DELETE', '/api/files/{file_uuid}/hidden'),
    'files.reprocess': ('POST', '/api/files/{file_uuid}/reprocess'),
    # uploads
    'files.prepare': ('POST', '/api/files/prepare'),
    'files.upload': ('POST', '/api/files'),
    'files.status_detail': ('GET', '/api/files/{file_uuid}/status-detail'),
    'files.delete': ('DELETE', '/api/files/{file_uuid}'),
    'files.purge': ('DELETE', '/api/files/{file_uuid}/purge'),
    # chat
    'llm.status': ('GET', '/api/llm/status'),
    'llm.create': ('POST', '/api/llm-settings'),
    'llm.delete': ('DELETE', '/api/llm-settings/config/{config_uuid}'),
    'chat.list': ('GET', '/api/chat/conversations'),
    'chat.get': ('GET', '/api/chat/conversations/{conversation_uuid}'),
    'chat.messages': ('GET', '/api/chat/conversations/{conversation_uuid}/messages'),
    'chat.create': ('POST', '/api/chat/conversations'),
    'chat.send': ('POST', '/api/chat/conversations/{conversation_uuid}/messages'),
    'chat.delete': ('DELETE', '/api/chat/conversations/{conversation_uuid}'),
    # administrator
    'admin.stats': ('GET', '/api/admin/stats'),
    'system.stats': ('GET', '/api/system/stats'),
    'admin.users': ('GET', '/api/admin/users'),
    'admin.audit_logs': ('GET', '/api/admin/audit-logs'),
    'admin.quarantined': ('GET', '/api/admin/files/quarantined'),
    'admin.holds': ('GET', '/api/admin/holds'),
    'admin.hold': ('GET', '/api/admin/holds/{hold_uuid}'),
    'admin.locked_accounts': ('GET', '/api/admin/locked-accounts'),
    'admin.user_approvals': ('GET', '/api/admin/user-approvals'),
    'admin.retention_config': ('GET', '/api/admin/settings/retention-config'),
    'admin.retry_config': ('GET', '/api/admin/settings/retry-config'),
    'admin.cache_config': ('GET', '/api/admin/settings/cache-config'),
    'admin.transcription_defaults': ('GET', '/api/admin/transcription-defaults'),
    'admin.chat_settings': ('GET', '/api/admin/chat-settings'),
    'admin.redaction_policy': ('GET', '/api/admin/redaction-policy'),
    'admin.takedowns': ('GET', '/api/admin/takedown-notices'),
    'admin.timing_recent': ('GET', '/api/admin/timing-summary/recent'),
    'admin.auth_config_status': ('GET', '/api/admin/auth-config/status'),
    'admin.first_run': ('GET', '/api/admin/first-run-wizard/status'),
    'admin.group_mappings': ('GET', '/api/admin/group-mappings'),
    'admin.scim_tokens': ('GET', '/api/admin/scim-tokens'),
    'tasks.system_health': ('GET', '/api/tasks/system/health'),
    'files.stuck': ('GET', '/api/files/management/stuck'),
    'search.index_health': ('GET', '/api/search/index-health'),
    # organization administrator (capability-gated; off in the community edition)
    'org.audit_logs': ('GET', '/api/org-admin/audit-logs'),
    'org.holds': ('GET', '/api/org-admin/holds'),
    'org.hold': ('GET', '/api/org-admin/holds/{hold_uuid}'),
    'org.quarantined': ('GET', '/api/org-admin/files/quarantined'),
    'org.policy': ('GET', '/api/org-admin/policy'),
    'org.erasures': ('GET', '/api/org-admin/gdpr/erasures'),
}

# Not an OpenAPI path (FastAPI leaves WebSocket routes out of the schema); the test checks the
# app mounts it.
WS_PATH = '/api/ws'

# The org-admin routes, keyed by the capability that mounts them (app/core/capabilities.py).
ORG_ROUTE_CAPABILITY: dict[str, str] = {
    'org.audit_logs': 'org.audit_logs',
    'org.holds': 'org.legal_hold',
    'org.hold': 'org.legal_hold',
    'org.quarantined': 'org.takedown',
    'org.policy': 'organizations',
    'org.erasures': 'organizations',
}

# OpenAPI tag group -> the personas whose tasks call it. The test derives the same map from a
# recorded run and requires equality, so this table cannot drift from the code.
GROUP_PERSONAS: dict[str, tuple[str, ...]] = {
    'admin': ('Administrator',),
    'admin-takedown-notices': ('Administrator',),
    'admin-timing': ('Administrator',),
    'asr-settings': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'auth': (
        'Viewer',
        'HeavyOwner',
        'Editor',
        'ChatUser',
        'Administrator',
        'OrgAdmin',
        'WebSocketListener',
        'PipelineUploader',
        'Researcher',
        'SpeakerCurator',
        'TeamLead',
        'StatusPoller',
        'WatcherBot',
        'LoginHerd',
        'HotFileReader',
    ),
    'auth-config': ('Administrator',),
    'chat': (
        'ChatUser',
        'Researcher',
    ),
    'chat-settings': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'collections': (
        'Viewer',
        'HeavyOwner',
        'Editor',
        'Administrator',
        'Researcher',
        'TeamLead',
    ),
    'comments': (
        'Viewer',
        'HeavyOwner',
        'Editor',
        'Administrator',
        'Researcher',
        'HotFileReader',
    ),
    'custom-vocabulary': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'diarization-settings': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'file-management': (
        'Editor',
        'Administrator',
        'PipelineUploader',
        'StatusPoller',
    ),
    'files': (
        'Viewer',
        'HeavyOwner',
        'Editor',
        'ChatUser',
        'Administrator',
        'OrgAdmin',
        'WebSocketListener',
        'PipelineUploader',
        'Researcher',
        'SpeakerCurator',
        'TeamLead',
        'StatusPoller',
        'WatcherBot',
        'LoginHerd',
        'HotFileReader',
    ),
    'first-run-wizard': ('Administrator',),
    'group-mappings': ('Administrator',),
    'groups': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'legal': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'llm-settings': (
        'Viewer',
        'HeavyOwner',
        'ChatUser',
        'Administrator',
        'Researcher',
    ),
    'llm-status': (
        'ChatUser',
        'Researcher',
    ),
    'org-admin': ('OrgAdmin',),
    'pipeline-settings': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'privacy': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'prompts': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'redaction-settings': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'scim-tokens': ('Administrator',),
    'search': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
        'Researcher',
    ),
    'speaker-clusters': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
        'SpeakerCurator',
    ),
    'speaker-profiles': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
        'SpeakerCurator',
    ),
    'speakers': (
        'Viewer',
        'HeavyOwner',
        'Editor',
        'Administrator',
        'Researcher',
        'SpeakerCurator',
    ),
    'summarization': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
        'TeamLead',
    ),
    'system': (
        'Viewer',
        'HeavyOwner',
        'Editor',
        'ChatUser',
        'Administrator',
        'OrgAdmin',
        'WebSocketListener',
        'PipelineUploader',
        'Researcher',
        'SpeakerCurator',
        'TeamLead',
        'StatusPoller',
        'WatcherBot',
        'LoginHerd',
        'HotFileReader',
    ),
    'tags': (
        'Viewer',
        'HeavyOwner',
        'Editor',
        'Administrator',
        'Researcher',
        'TeamLead',
    ),
    'tasks': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
        'TeamLead',
        'StatusPoller',
    ),
    'topics': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
        'TeamLead',
    ),
    'transcript-segments': (
        'Editor',
        'SpeakerCurator',
    ),
    'usage': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'user-files': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
        'TeamLead',
        'StatusPoller',
    ),
    'user-settings': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'users': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
    'version': (
        'Viewer',
        'HeavyOwner',
        'Administrator',
    ),
}

# OpenAPI tag groups the load test deliberately does not call, and why.
EXCLUDED_GROUPS: dict[str, str] = {
    '-': 'container health probes (/health, /health/ready), polled by Docker, not by people',
    'admin-storage-integrity': 'an operator storage audit that walks object storage',
    'combined-speaker-migration': 'one-off administrator data migration that rewrites speakers',
    'directory-sync': 'needs a configured LDAP/SCIM directory to sync from',
    'embedding-migration': 'one-off administrator re-embedding of every voiceprint',
    'scim': 'machine-to-machine user provisioning with a SCIM bearer token, not a browser user',
    'speaker-attribute-migration': 'one-off administrator data migration',
    'support-access': 'support-access grants are a hosted-edition operator workflow',
    'takedown-notices': 'per-user takedown list, rate limited per person and empty on the seed',
    'watch-sources': 'needs a watched folder, S3 bucket or SMB share to exist',
}

# The users of one identity block (``scenario.pool_bounds``), for SEED_NEEDS.
_POOL = (
    'SELECT id FROM "user" WHERE email LIKE \'perf-%@loadtest.invalid\' '
    "AND substring(email from 'perf-([0-9]+)@')::int BETWEEN :lo AND :hi"
)
_OWN_FILES = f'SELECT id FROM media_file WHERE user_id IN ({_POOL}) AND deleted_at IS NULL'

# persona -> (identity block, [(what the tasks need, SQL counting it; must be > 0)]).
SEED_NEEDS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    'Viewer': (
        'viewer',
        [
            (
                'own files with transcript segments',
                f'SELECT count(DISTINCT media_file_id) FROM transcript_segment WHERE media_file_id IN ({_OWN_FILES})',
            ),
            (
                'speakers on own files',
                f'SELECT count(*) FROM speaker WHERE media_file_id IN ({_OWN_FILES})',
            ),
            (
                'comments on own files',
                f'SELECT count(*) FROM comment WHERE media_file_id IN ({_OWN_FILES})',
            ),
            (
                'personal tags on own files',
                f'SELECT count(*) FROM file_tag ft JOIN tag t ON t.id = ft.tag_id WHERE t.user_id IN ({_POOL}) AND ft.media_file_id IN ({_OWN_FILES})',
            ),
            (
                'own collections with files',
                f'SELECT count(*) FROM collection_member WHERE collection_id IN (SELECT id FROM collection WHERE user_id IN ({_POOL}))',
            ),
            (
                'own speaker profiles',
                f'SELECT count(*) FROM speaker_profile WHERE user_id IN ({_POOL})',
            ),
            (
                'cached waveforms on own files',
                f'SELECT count(*) FROM media_file WHERE id IN ({_OWN_FILES}) AND waveform_data IS NOT NULL',
            ),
            (
                'own files in the trash',
                f'SELECT count(*) FROM media_file WHERE user_id IN ({_POOL}) AND deleted_at IS NOT NULL',
            ),
        ],
    ),
    'Editor': (
        'editor',
        [
            (
                'own unheld files with segments',
                f'SELECT count(DISTINCT media_file_id) FROM transcript_segment WHERE media_file_id IN ({_OWN_FILES} AND NOT legal_hold AND NOT is_quarantined)',
            ),
            (
                'speakers to merge into',
                f'SELECT count(*) FROM speaker WHERE media_file_id IN ({_OWN_FILES})',
            ),
        ],
    ),
    'ChatUser': (
        'chat',
        [
            (
                'own files to scope a chat to',
                f'SELECT count(*) FROM media_file WHERE id IN ({_OWN_FILES})',
            ),
            (
                'chat history with messages',
                f'SELECT count(*) FROM chat_message WHERE conversation_id IN (SELECT id FROM chat_conversation WHERE user_id IN ({_POOL}))',
            ),
        ],
    ),
    'WebSocketListener': ('ws', [('identities', f'SELECT count(*) FROM ({_POOL}) p')]),
    'WatcherBot': ('ws', [('identities', f'SELECT count(*) FROM ({_POOL}) p')]),
    'StatusPoller': ('ws', [('identities', f'SELECT count(*) FROM ({_POOL}) p')]),
    'Researcher': (
        'viewer',
        [
            (
                'own files with transcript segments (the phrasebook is learned from them)',
                f'SELECT count(DISTINCT media_file_id) FROM transcript_segment WHERE media_file_id IN ({_OWN_FILES})',
            ),
            (
                'own collections with files (chat scope)',
                f'SELECT count(*) FROM collection_member WHERE collection_id IN (SELECT id FROM collection WHERE user_id IN ({_POOL}))',
            ),
        ],
    ),
    'SpeakerCurator': (
        'editor',
        [
            (
                'own unheld files with segments',
                f'SELECT count(DISTINCT media_file_id) FROM transcript_segment WHERE media_file_id IN ({_OWN_FILES} AND NOT legal_hold AND NOT is_quarantined)',
            ),
            (
                'speakers to merge into',
                f'SELECT count(*) FROM speaker WHERE media_file_id IN ({_OWN_FILES})',
            ),
        ],
    ),
    'TeamLead': (
        'editor',
        [
            (
                'own unheld files',
                f'SELECT count(*) FROM media_file WHERE id IN ({_OWN_FILES} AND NOT legal_hold AND NOT is_quarantined)',
            ),
        ],
    ),
    'LoginHerd': ('viewer', [('identities', f'SELECT count(*) FROM ({_POOL}) p')]),
    'HotFileReader': (
        'heavy',
        [
            (
                "the heavy owner's files (the hot file is its oldest)",
                'SELECT count(*) FROM media_file m JOIN "user" u ON u.id = m.user_id WHERE u.email = \'perf-2@loadtest.invalid\'',
            ),
        ],
    ),
    'PipelineUploader': ('pipeline', [('identities', f'SELECT count(*) FROM ({_POOL}) p')]),
    'Administrator': (
        'admin',
        [
            (
                'the super admin account (audit logs are super-admin only)',
                "SELECT count(*) FROM \"user\" WHERE email = 'perf-1@loadtest.invalid' AND role = 'super_admin'",
            ),
            (
                'legal holds',
                "SELECT count(*) FROM content_hold WHERE reason LIKE 'loadtest hold %'",
            ),
            (
                'quarantined files',
                "SELECT count(*) FROM media_file WHERE is_quarantined AND storage_path LIKE 'loadtest/%'",
            ),
        ],
    ),
    'OrgAdmin': (
        'org_admin',
        [
            (
                'an organization admin',
                "SELECT count(*) FROM organization_membership m JOIN \"user\" u ON u.id = m.user_id WHERE u.email = 'perf-3@loadtest.invalid' AND m.role = 'org:admin'",
            ),
        ],
    ),
    'HeavyOwner': (
        'heavy',
        [
            (
                "the heavy owner's files",
                'SELECT count(*) FROM media_file m JOIN "user" u ON u.id = m.user_id WHERE u.email = \'perf-2@loadtest.invalid\'',
            ),
        ],
    ),
}
