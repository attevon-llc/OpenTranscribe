/**
 * TypeScript types for tags and tag management.
 *
 * Mirrors the Pydantic schemas in `backend/app/schemas/media.py` (Tag,
 * TagWithCount, TagCollisionCluster, TagImpact*, TagMutationResult,
 * TagReview*) and the bulk tag envelope in
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
 * `is_shared` marks a system tag (`user_id IS NULL` on the backend): the seeded
 * vocabulary every account sees and attaches to. Only an admin may rename,
 * merge or delete one.
 */
export interface Tag {
  uuid: string;
  name: string;
  /** How the tag was created — e.g. `auto_ai` for LLM-suggested tags. */
  source?: string | null;
  /** True for a system tag in the shared vocabulary. */
  is_shared?: boolean;
}

/**
 * A tag plus the counts the list UI renders.
 *
 * `usage_count` is scoped to the files the caller can access — the `unused`
 * filter is its exact complement. `awaiting_review` is pre-computed by the
 * backend so the badge is never re-derived from the source string here.
 */
export interface TagWithCount extends Tag {
  usage_count: number;
  awaiting_review: boolean;
}

/** Server-side filters for the tag list; they combine (AND). */
export interface TagListFilters {
  awaiting_review?: boolean;
  unused?: boolean;
  colliding?: boolean;
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

/** Accept endorses an auto-labeled tag; reject drops the associations it created. */
export type TagReviewAction = 'accept' | 'reject';

/**
 * Per-tag outcome of an accept / reject. `not_applicable` covers a tag the
 * auto-labeler did not create — reported rather than failing the call.
 */
export type TagReviewOutcome = 'accepted' | 'rejected' | 'not_applicable';

export interface TagReviewEntry {
  uuid: string;
  name: string;
  outcome: TagReviewOutcome;
  removed_association_count: number;
  retained_association_count: number;
  tag_removed: boolean;
}

/**
 * Outcome of an accept / reject, or of the preview that precedes it
 * (`applied` is false for a preview).
 *
 * The two association counts stay separate: rejecting removes only what the
 * auto-labeler applied, so one number could not distinguish a reject that
 * clears a tag from one that leaves most of it in place.
 */
export interface TagReviewResult {
  tags: TagReviewEntry[];
  removed_association_count: number;
  retained_association_count: number;
  deleted_uuids: string[];
  applied: boolean;
  impact: TagImpact;
}

/** Result of the admin-only global cleanup of tags no file carries. */
export interface TagCleanupResult {
  deleted_count: number;
  message: string;
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
