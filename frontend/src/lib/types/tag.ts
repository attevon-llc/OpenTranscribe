/**
 * TypeScript types for tags and tag management.
 *
 * Mirrors the Pydantic schemas in `backend/app/schemas/media.py` (Tag,
 * TagWithCount, TagCollisionCluster, TagImpact*, TagMutationResult,
 * and the bulk tag envelope in
 * `backend/app/api/endpoints/files/management.py`.
 */

/**
 * Canonical tag shape, mirroring `backend/app/schemas/media.py:Tag`.
 *
 * Import via `$lib/types/tag`. This is the **only** tag shape on the API:
 * `/tags`, `POST /tags/files/{uuid}/tags` and `MediaFileDetail.tags` all serve
 * it (#326). The gallery list endpoint `GET /files` sends no tags at all, which
 * is why `MediaFile` has no `tags` field — only `MediaFileDetail` does.
 *
 * `ownership` says what the caller may do with the tag — see `TagOwnership`.
 */
export interface Tag {
  uuid: string;
  name: string;
  /** How the tag was created — e.g. `auto_ai` for LLM-suggested tags. */
  source?: string | null;
  /** The caller's relationship to this tag, and therefore their rights over it. */
  ownership?: TagOwnership;
}

/**
 * A tag plus the counts the list UI renders.
 *
 * `usage_count` is scoped to the files the caller can access — the `unused`
 * filter is its exact complement.
 */
export interface TagWithCount extends Tag {
  usage_count: number;
}

/**
 * The caller's relationship to a tag, mirroring `tag_service.TAG_OWNERSHIPS`.
 *
 * - `mine` — they own it; full control.
 * - `system` — the shared vocabulary every account sees; admin-only to change.
 * - `shared_with_me` — another account's, visible only because it sits on a
 *   file shared with them. Read-only: every mutation answers 404, so the UI
 *   must not offer one.
 */
export type TagOwnership = 'mine' | 'system' | 'shared_with_me';

/** True when the caller may rename / merge / delete this tag. */
export function canMutateTag(tag: Tag, isAdmin: boolean): boolean {
  const ownership = tag.ownership ?? 'mine';
  if (ownership === 'shared_with_me') return false;
  if (ownership === 'system') return isAdmin;
  return true;
}

/**
 * Ownership scope for the tag list — `all`, or one `TagOwnership` value.
 *
 * Deliberately the same vocabulary the rows report: a scoped request returns
 * only rows whose `ownership` equals the scope, so the filter and the field it
 * filters on cannot drift. A separate axis from the three view filters below,
 * so "my unused tags" is expressible.
 */
export type TagScope = 'all' | TagOwnership;

/** A file carrying a tag, as the manager's "what it touches" list renders it. */
export interface TaggedFile {
  uuid: string;
  display_title: string;
  status?: string | null;
  formatted_duration?: string | null;
}

/** The files a tag touches. `total` is the real count; `files` is capped. */
export interface TagFileList {
  files: TaggedFile[];
  total: number;
}

/** Server-side filters for the tag list; they combine (AND). */
export interface TagListFilters {
  unused?: boolean;
  colliding?: boolean;
  scope?: TagScope;
}

/** A tag sharing its normalized name with the rest of its cluster. */
export interface TagClusterMember extends Tag {
  usage_count: number;
  suggested_survivor: boolean;
}

/** A near match offered beside a cluster — a prompt for a human, never a member. */
export interface TagClusterSuggestion extends Tag {
  usage_count: number;
  similarity: number;
}

/** Tags that normalize to one name, with the merge the backend recommends. */
export interface TagCollisionCluster {
  normalized_name: string;
  members: TagClusterMember[];
  suggested_survivor_uuid: string | null;
  suggestions: TagClusterSuggestion[];
}

/**
 * File counts for a single tag in a pending rename / merge / delete.
 *
 * The two counts are deliberately separate: a shared tag reaches files you
 * cannot see, so `accessible_file_count` is what the caller can see while
 * `total_file_count` is what the operation actually changes. Carry both
 * through — a confirmation built from the accessible number alone would
 * read "3 files" in front of a delete that strips the tag from 500.
 */
export interface TagImpactEntry {
  uuid: string;
  name: string;
  accessible_file_count: number;
  total_file_count: number;
}

/** What a destructive tag operation would touch, before it acts. */
export interface TagImpact {
  tags: TagImpactEntry[];
  accessible_file_count: number;
  total_file_count: number;
}

/** Rename a tag; `confirm_merge` accepts the merge when the new name collides. */
export interface TagRenameRequest {
  name: string;
  confirm_merge?: boolean;
}

/** Fold one or more tags into the tag named in the path. */
export interface TagMergeRequest {
  source_uuids: string[];
}

/**
 * Outcome of a rename / merge / delete, always carrying the impact.
 *
 * A rename whose new name resolves to a different existing tag comes back
 * with `requires_confirmation` and applies nothing until the caller retries
 * with `confirm_merge`.
 */
export interface TagMutationResult {
  tag: Tag | null;
  merged: boolean;
  requires_confirmation: boolean;
  deleted_uuids: string[];
  impact: TagImpact;
}


/** Bulk tagging rides the files rail (`/files/management/bulk-action`). */
export type BulkTagAction = 'add_tag' | 'remove_tag';

/**
 * What a bulk tag action did to one file. `already_present` / `not_present`
 * are *successful* no-ops — the file ends in the requested state without
 * having to change. Only `failed` means it did not get there.
 */
export type BulkTagOutcome = 'added' | 'already_present' | 'removed' | 'not_present' | 'failed';

export interface BulkTagActionResult {
  file_uuid: string;
  success: boolean;
  message: string;
  error: string | null;
  outcome: BulkTagOutcome | null;
}
