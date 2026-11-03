# api/endpoints/tags — the tag plane

Moved intact from `backend/app/api/CLAUDE.md`. The frontend side is `frontend/src/components/tags/CLAUDE.md`.

- **`endpoints/tags/` is a package** (`crud` · `discovery` · `sharing` · `operations` + `_common`),
  split like `endpoints/files/`. Every module registers onto the ONE router in `_common` —
  sub-routers were tried and rejected because FastAPI refuses an empty path when including a
  router with no prefix, and `POST ""`/`GET ""` are real routes. **Import order in
  `__init__` is load-bearing**: `/for-files`, `/impact` and `/promote` are literal paths that
  must register before the `/{tag_uuid}` routes. Do NOT re-export third-party names through
  `_common` — ruff prunes them as unused there and the backend stops booting.
- **The tag plane has THREE scopes, and mixing them up is the bug** (`v374_add_tag_user_id`).
  The first two live in `services/tag_scope.py` so nothing re-derives them:
  | Scope | Rule | Used by |
  |---|---|---|
  | `visible_to()` | system **or** owned **or** explicitly shared (`v386`) **or** attached to an accessible file | `GET /tags` (the list) |
  | `owned_or_system()` | system **or** owned | resolution, `/unused`, `/collisions` |
  | `_writable_tag_ids()` | owned always; system for an **admin**; else **404** | every mutation |
  Reading and rewriting are different rights: `visible_to` admits a tag on a file shared with
  you, but renaming it rewrites its owner's vocabulary everywhere. Narrowing the *list* to
  `owned_or_system` silently drops shared-file tags from the recipient's picker; widening
  *mutation* to `visible_to` lets anyone rename anyone's tag. The non-writable case answers
  404, not 403, so probing cannot enumerate other accounts' tags.
- **Tags are per-owner; the shared tier is `user_id IS NULL`.** `resolve_or_create_tag` takes a
  **required** `user_id` — an ownerless row is a *system* tag, published to every account, and
  only `initial_data._ensure_default_tags` may create one. Lookup prefers the caller's row, then
  a same-named system row, so applying a seeded default attaches the shared row. `POST
  /tags/promote` (admin) moves an owned tag into that tier and folds same-named rows into it.
  Names are unique only **per owner**, so `remove_tag_from_file` resolves via `FileTag` for that
  file, never by name.
- **One file never carries the same word twice — but only where `file_id` is passed.**
  `resolve_or_create_tag(..., file_id=...)` consults `lookup_tag_on_file` first, so the second
  person to tag a shared file reuses the row instead of forking a same-named one. Without it the
  detail page renders the tag twice and the gallery's ALL-filter has to count `DISTINCT Tag.name`
  to compensate. **Verified Aug 2026: `services/auto_label_service.py` is the ONLY caller that
  passes it.** The interactive path `POST /tags/files/{uuid}/tags` → `tags/_common._resolve_tag`
  omits it, and so does `services/tag_bulk.py`, so those dedupe by tag *id* on `FileTag` — a
  repeat post by one user is idempotent, but two users can attach same-named rows of their own to
  one shared file. Treat this as an invariant the auto-label path upholds and the human path does
  not, not as a global guarantee.
- **A tag share grants vocabulary, not administration** (`v386_add_tag_share`). `tag_share`
  mirrors `collection_share` — one user or one group, CHECK-constrained, partial unique indexes —
  but deliberately has **no permission column**: the recipient can see, filter by and apply the
  tag while rename/merge/delete stay with the owner, so a `viewer`/`editor` distinction would be
  a field pretending to be a choice.
- **Bulk tag paths must stay batched.** `prepare_upload.add_tags_to_file` goes through
  `resolve_or_create_tags` (constant SELECTs, issue #284 A2.8), not the per-name resolver;
  `tests/api/test_upload_prep_batching.py` fails if the per-name loop returns.
- **`DELETE /tags/cleanup` defaults to the CALLER's tags; the deployment-wide sweep must be asked
  for twice.** It used to delete every owned tag (`user_id IS NOT NULL`) unreferenced by any
  `FileTag` across all users and orgs, while its inspection sibling `GET /tags/unused` is
  caller-scoped — so an admin who read the list and then called `/cleanup` irreversibly deleted
  rows they were never shown, with no impact preview and no parameter that could have warned them
  (#431). Now: `scope=mine` (**the default**) sweeps only the caller's rows; `scope=all_users`
  needs `confirm=true` as well or it is a **400**, the same double opt-in as
  `POST /org-admin/gdpr/erase-organization`. The response gained `scope`; `deleted_count` and
  `message` are unchanged. The sweep lives in `services/tag_operations.cleanup_unreferenced_tags`
  and measures "unreferenced" **globally**, not against accessible files — an owned tag can sit on
  a file the owner can no longer see, and deleting it would strip the tag off someone else's file,
  so `/unused` may list a tag `/cleanup` declines to delete. System tags are exempt in both
  scopes. Still gated by an **inline** `is_admin` check rather than a dependency, so a
  dependency-based authz audit does not see it.
