# Tenant boundaries

Who can see, use and change what, per resource, and where the code enforces it. Keep this
table current when a resource or a sharing path is added: the guard tests in
`backend/tests/api/test_cross_tenant_sharing.py` (`test_every_shareable_model_is_covered`,
`test_every_shared_setting_key_is_covered`) fail when a model with an `is_shared` column,
a user/group grant table, or a shared `UserSetting` key is added without a cross-tenant test.

## The rule

A request runs in one **tenant**, carried by `RequestContext` (`backend/app/api/deps_context.py`):

- `ctx.org_id` is an int: the caller's **active organization**. Only rows of that organization
  are reachable; shared per-user items only when their owner is a member of it.
- `ctx.org_id` is `None`: the caller's **personal workspace**. Only organization-less rows;
  shared per-user items only when their owner belongs to no organization.
- **Community installs** have no organizations, so everything is organization-less and every
  account is "personal": instance-wide sharing and the pre-tenancy behaviour are unchanged.
- **Background work** has no request context. It takes the tenant from the row it is working
  on (a file's `organization_id`), or, for a stored pointer to another user's shared item, asks
  whether owner and user share a tenant (`owner_shares_tenant_with`).
- Rows in another tenant answer **404**, not 403, so their existence is not confirmed.
- Instance administrators (`is_admin`) keep instance-wide operational access by design.
  Organization administrators (`require_org_admin`) act only inside their organization.

Shared helpers: `backend/app/utils/tenant_sharing.py` (`owner_in_tenant`,
`shared_visible_in_tenant` for request paths, `shared_usable_by` for worker paths,
`user_in_tenant`, `group_members_outside_org`), `backend/app/services/permission_service.py`
(`get_accessible_file_ids_subquery`, `get_file_permission`, `get_collection_permission`,
`collection_tenant_pred`, `get_accessible_profile_ids`, all taking `organization_id`; the
`UNSCOPED` sentinel is only for deliberate cross-scope ownership checks), and
`backend/app/utils/uuid_helpers.py` (`get_file_by_uuid_with_permission`,
`require_speaker_access`, `require_profile_in_scope`).

## Table

Paths are relative to `backend/`. Test abbreviations: **XT** =
`tests/api/test_cross_tenant_sharing.py`, **TI** = `tests/test_tenant_isolation.py`,
**CT** = `tests/api/test_collection_tenancy.py`, **SP** = `tests/api/test_speaker_tenancy.py`,
**OL** = `tests/api/test_owner_listing_tenancy.py`, **DS** =
`tests/api/test_dedup_sharing_tenancy.py`.

### People and membership

| Resource | Who can see | Who can use / modify | Enforced at | Proving test |
|---|---|---|---|---|
| Users (directory search) | Members of the active org; in personal scope only accounts with no org; emails masked | n/a (admin-only user list/detail) | `app/api/endpoints/users.py:search_users` | `tests/api/endpoints/test_users.py::test_user_search_does_not_leak_another_tenants_accounts`, `::test_user_search_in_personal_scope_omits_org_members` |
| Org members / seats | Org admins of that org | Org admins of that org (erase member, audit log) | `app/api/deps_context.py:require_org_admin`, `app/api/endpoints/org_admin.py:_org_member_user_ids` | `tests/api/endpoints/test_org_admin_endpoints.py::test_target_in_another_org_is_403`, `::test_plain_org_member_cannot_read_the_org_audit_log` |
| Groups | Groups of the active tenant the caller belongs to | Group owner/admin; new members must be in the same tenant | `app/api/endpoints/groups.py:_get_group_in_scope`, `_same_tenant` → `tenant_sharing.user_in_tenant` | `tests/api/test_groups_tenancy.py::test_a_group_in_another_tenant_is_404`, `::test_add_member_refuses_a_target_from_another_tenant` |

### Media and content

| Resource | Who can see | Who can use / modify | Enforced at | Proving test |
|---|---|---|---|---|
| Files (list, detail, stream, download, segments, subtitles, waveform, exports) | Owner's files in the active tenant, plus files of collections shared with the caller in that tenant | Owner; collection editors | `permission_service.get_accessible_file_ids_subquery`, `uuid_helpers.get_file_by_uuid_with_permission` | TI `TestSqlFileAccess::test_org_a_cannot_reach_org_b_file_by_uuid`, `TestApiReadSurfacesPersonalScope::test_org_stamped_file_blocked_on_read_surfaces`; `tests/api/test_file_detail_not_found.py::test_another_users_file_is_indistinguishable_from_a_missing_one` |
| File sharing (via collections) | Share recipients, inside the collection's org | Collection owner; org collections only to org members / all-member groups | `app/api/endpoints/media_collections.py:create_collection_share` | TI `TestCrossOrgCollectionShareApi::test_org_collection_share_to_non_member_rejected`, `TestGroupShareOrgMembership` |
| Collections | Org collections: every member of that org; personal: owner; plus shared-with-me in the active tenant | Org: creator / org admin owner, members editor; shares per grant | `permission_service.collection_tenant_pred`, `get_collection_permission` | CT `::test_collection_in_one_org_is_invisible_in_another_org_and_personal`, `::test_shared_with_me_is_tenant_gated` |
| Collection share notifications | Org collection: only recipients who are members of its org | n/a | `media_collections.py:_get_share_target_user_ids` | DS `::test_org_collection_share_notifies_only_org_members` |
| Tags | Tags of the active tenant, system tags, tags shared with the caller or on accessible files, all bounded to the active tenant | Tenant-owned tags: owner / org members per `_writable_tag_ids` | `app/services/tag_service.py:visible_to`, `app/api/endpoints/tags/_common.py:_writable_tag_ids` | `tests/api/test_tag_tenancy.py::test_tag_created_in_one_org_is_not_listed_in_another_or_personal`, `::test_tag_from_another_tenant_cannot_be_mutated`; XT `::test_tag_grant_stays_in_the_tags_tenant` |
| Tag shares | Recipient, only in the tag's tenant | Org tag: target user must be an org member; group target: sharer in the group and every member in the org | `app/services/tag_sharing.py:share_tag` | DS `::test_org_tag_user_share_requires_org_membership`, `::test_org_tag_group_share_requires_every_member_in_org` |
| Comments | Anyone with access to the file in the active tenant | Author (edit/delete); file access to add | `app/api/endpoints/comments.py:_check_file_access` | `tests/api/endpoints/test_comments.py::test_a_stranger_cannot_read_a_comment` |
| Upload / URL / watch-source dedup | Only the caller's (or source's) tenant is searched for duplicates | Failed-duplicate cleanup touches only own files in the active tenant | `app/utils/file_hash.py:check_duplicate_by_fingerprint`, `check_duplicate_by_imohash`, `cleanup_failed_duplicates`; `files/url_processing.py:_check_duplicate_video` | DS `::test_prepare_upload_duplicate_is_tenant_scoped`, `::test_watch_import_dedups_against_existing_media_only_in_tenant`, `::test_url_duplicate_is_tenant_scoped` |
| Watch sources | Owner, in the source's tenant | Owner in that tenant; imports stop and the source is disabled once the owner leaves its org | `app/api/endpoints/watch_sources.py:_get_source_or_404`, `list_watch_sources`; `app/services/watch_sources/processing.py:disable_if_owner_left_org` | DS `::test_watch_source_detail_is_tenant_scoped`, `::test_watch_import_is_refused_and_source_disabled_after_owner_leaves_org` |
| Bulk export stream | Only the user who prepared the job | n/a | `app/api/endpoints/files/subtitles.py:bulk_job_owned_by` | OL `::test_bulk_export_job_is_bound_to_the_user_who_prepared_it` |

### Speakers and voiceprints

| Resource | Who can see | Who can use / modify | Enforced at | Proving test |
|---|---|---|---|---|
| Speakers | Anyone with access to the speaker's file in the active tenant | Editor/owner on that file in the active tenant | `uuid_helpers.require_speaker_access` | SP `::test_speaker_routes_are_confined_to_the_speakers_tenant`, `::test_speaker_listing_without_a_file_shows_only_the_active_tenant` |
| Speaker profiles | Owner or share recipient, in the profile's tenant | Owner, in the profile's tenant | `uuid_helpers.require_profile_in_scope`, `permission_service.get_accessible_profile_ids_with_source` | SP `::test_profile_routes_are_confined_to_the_profiles_tenant`, `::test_profile_listing_shows_only_the_active_tenant` |
| Voiceprint links (speaker → profile, verify, merge) | n/a | Editor on the speaker's file; profile in the same tenant as that file; merges within one tenant and one file or owner | `speaker_profiles.py:assign_speaker_to_profile`, `speakers.py:_resolve_profile_uuid_to_id`, `speakers.py:merge_speakers` | SP `::test_assign_profile_needs_editor_on_the_speakers_file`, `::test_assign_profile_refuses_a_profile_of_another_tenant`, `::test_merge_refuses_speakers_of_two_tenants` |
| Profile embeddings (consolidated voiceprint) | n/a (kNN is filtered by org + accessible profile ids) | Averages only speakers whose file is in the profile's tenant | `app/services/profile_embedding_service.py:update_profile_embedding`, `_execute_knn_search` | SP `::test_profile_embedding_averages_only_speakers_of_the_profiles_tenant`; TI `TestSpeakerFilterOrgGate` |
| Speaker clusters, inbox, media preview | Owner, in the cluster's tenant | Owner; promote/merge only single-tenant clusters | `app/services/speaker_clustering_service.py:_scoped_cluster_query`, `speaker_clusters.py` | SP `::test_cluster_routes_are_confined_to_the_clusters_tenant`, `::test_promote_refuses_a_cluster_whose_members_span_tenants` |
| Speaker collections | Owner, in the collection's tenant | Owner; created in the active tenant | `speaker_profiles.py:list_speaker_collections`, `create_speaker_collection` | SP `::test_speaker_collections_are_stamped_and_listed_per_tenant`, `::test_a_name_can_be_reused_in_another_tenant_but_not_twice_in_one` |
| Background labelling (auto profile, retroactive matching) | n/a | Only profiles/candidates in the tenant of the labelled speaker's file | `app/tasks/speaker_update.py:auto_create_or_assign_profile`, `trigger_retroactive_matching` | SP `::test_auto_profile_does_not_link_a_same_named_profile_of_another_tenant`, `::test_retroactive_matching_scores_only_same_tenant_candidates` |

### AI features

| Resource | Who can see | Who can use / modify | Enforced at | Proving test |
|---|---|---|---|---|
| Chat conversations / projects / messages | Owner, in the conversation's tenant | Owner, in that tenant (incl. cancel) | `app/api/endpoints/chat/common.py:get_owned_conversation`, `chat/projects.py:get_owned_project`, `chat/messages.py:cancel_message` | `tests/test_chat_endpoints.py::test_another_users_conversation_is_404_not_403`; OL `::test_chat_cancel_requires_the_conversations_tenant` |
| Chat / RAG context | Only files accessible in the conversation's tenant feed the model | n/a | `app/services/chat/context_resolver.py:_resolve_explicit_files`, `hybrid_search_service.py:_build_filters` | `tests/test_chat_context_resolver.py::test_another_users_collection_resolves_to_nothing`; CT `::test_chat_scope_resolves_a_colleagues_org_collection` |
| Search (hybrid, summaries, counts) | Documents of accessible files in the active tenant | n/a | `app/services/search/hybrid_search_service.py:_build_filters`, `summary_search.py:search_summaries` | TI `TestSearchFilterOrgGate`; `tests/api/endpoints/test_search.py::test_the_search_is_scoped_to_the_caller` |
| Search index health / reindex status | Admin: instance counts; user: own documents in the active tenant | n/a | `app/api/endpoints/search.py:get_index_health`, `reindex_status` | OL `::test_index_health_instance_counts_are_admin_only`, `::test_reindex_status_counts_only_the_active_tenant` |
| Summary prompts (shared) | System prompts; own; shared prompts whose owner is in the active tenant and that are not stamped for another org | Same set may be read, cloned, selected, or named on a summarize request; edit/delete owner only | `app/api/endpoints/prompts.py:_shared_in_tenant`, `_usable_by`, `require_usable_prompt_uuid`; worker `app/utils/prompt_manager.py:_resolve_active_prompt_record` | XT `::test_shared_listing[*-summary_prompt]`, `::test_shared_use[*-summary_prompt]`, `::test_shared_worker_pointer[*-summary_prompt]`, `::test_shared_prompt_library_scoped`, `::test_explicit_prompt_on_summarize_scoped` |
| LLM configurations (shared, carry keys) | Own; shared configs whose owner is in the active tenant; keys never lent | Same set may be selected / pinned to a chat; worker re-checks stored pointers | `app/api/endpoints/llm_settings.py:_visible_to`, `chat/common.py:resolve_llm_config_id`, `app/services/llm_service.py:_resolve_user_llm_settings`, `create_from_config_id` | XT `::test_shared_listing[*-llm_config]`, `::test_chat_llm_override_scoped`, `::test_chat_pinned_config_rechecked_at_message_time`, `::test_llm_test_connection_never_lends_a_shared_configs_key` |
| ASR configurations (shared, carry keys) | Own; shared configs whose owner is in the active tenant | Same; transcription-time provider re-checks the pointer | `app/api/endpoints/asr_settings.py:_get_config_or_404`, `app/services/asr/factory.py:create_for_user`, `get_active_model_capabilities` | XT `::test_shared_listing[*-asr_config]`, `::test_asr_provider_for_transcription_scoped` |
| Media sources (shared credentials) | Own; shared sources whose owner is in the active tenant | Worker uses only sources shared within a common tenant | `app/api/endpoints/user_settings.py:get_media_sources`, `app/services/protected_media_plugins/mediacms.py:_query_user_media_sources` | XT `::test_shared_listing[*-media_source]`, `::test_shared_worker_pointer[*-media_source]` |
| Organization context text (shared) | Own; shared contexts whose owner is in the active tenant | Same; summarization re-checks the "use shared from" pointer | `user_settings.py:get_shared_organization_contexts`, `use_shared_organization_context`; `app/tasks/summarization.py:_get_organization_context` | XT `::test_shared_listing[*-org_context]`, `::test_shared_worker_pointer[*-org_context]` |
| Custom vocabulary | Own terms of the active tenant + owner-less terms | Owner in that tenant; transcription uses the file's tenant | `app/api/endpoints/custom_vocabulary.py:_own_terms`, `_get_term_in_tenant`; `app/tasks/transcription/cloud_asr.py:load_vocabulary_terms` | OL `::test_custom_vocabulary_is_listed_exported_and_edited_in_its_tenant`, `::test_transcription_vocabulary_comes_from_the_files_tenant` |
| Topics / auto-labels | Via file access in the active tenant | Retroactive auto-label only on files of the active tenant | `app/api/endpoints/topics.py` → `get_file_by_uuid_with_permission`; `app/tasks/auto_labeling.py:pending_suggestion_ids` | OL `::test_retroactive_auto_label_only_picks_files_of_the_active_tenant` |

### Operations

| Resource | Who can see | Who can use / modify | Enforced at | Proving test |
|---|---|---|---|---|
| Tasks / file status / progress | Owner's files in the active tenant (admin: all) | Retry/recovery only on own files in the active tenant | `app/api/endpoints/tasks.py:_get_user_media_files`, `get_active_progress`; `user_files.py:get_user_file_status`; `task_detection_service.py:find_user_problem_files` | OL `::test_task_listing_and_lookup_follow_the_active_tenant`, `::test_my_files_status_counts_only_the_active_tenant`, `::test_active_progress_names_only_files_of_the_active_tenant` |
| Stuck files | Owner's files in the active tenant (admin: all) | n/a | `files/crud.py:get_media_file_by_id` via `files/management.py:get_stuck_files` | OL `::test_stuck_file_listing_follows_the_active_tenant` |
| LLM usage | Own events stamped with the active tenant | n/a | `app/api/endpoints/usage.py:get_my_usage`, `get_my_daily_usage` | OL `::test_usage_reports_only_the_active_tenant` |

## Known limits

- Speaker profile and speaker-collection names, and custom-vocabulary terms, are unique per user
  **per tenant** (`v430`; the personal workspace counts as one tenant). Rows created before
  tenant stamping were stamped by that migration where the evidence was unambiguous; the rest
  stay personal and are listed by `python -m app.scripts.backfill_tenant_stamps` for an
  administrator to decide.
- The `gpu_stats_update` websocket event carries instance hardware statistics to every socket;
  it holds no tenant data and matches what `GET /system/stats` already exposes.
