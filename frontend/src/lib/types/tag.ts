/**
 * Tag wire shapes, mirroring `backend/app/schemas/tag.py` (the bulk-action
 * envelope lives in `backend/app/api/endpoints/files/management.py`).
 *
 * Field names and literal unions are the JSON contract; keep them snake_case and
 * edit them by hand alongside the Pydantic models, since nothing generates them.
 */

/** How a tag relates to the caller, computed by the server per request. */
type TagOwnership = 'mine' | 'system' | 'shared_with_me';

export interface Tag {
  uuid: string;
  name: string;
  /** Origin of the tag: `manual`, `auto_ai`, `ai_accepted`, or null for built-in rows. */
  source?: string | null;
  ownership?: TagOwnership;
  /**
   * The server's verdict on whether it would accept this caller's rename, merge,
   * delete, impact preview and sharing of the tag. Every write control in the UI
   * keys off this; never re-derive it from the role or the ownership label.
   */
  can_write: boolean;
}

export interface TagWithCount extends Tag {
  usage_count: number;
}

/**
 * Client mirror of the server's deployment-scope check for system tags, used
 * only to decide whether to offer promote. Any admin may write system tags in a
 * single-tenant deployment; in a multi-tenant one only a super admin may. An
 * unknown tenancy is treated as multi-tenant so a plain admin is never offered a
 * write the server would refuse.
 */
export function canWriteSystemTags(
  role: string | undefined,
  tenancyMode: 'single' | 'multi' | undefined
): boolean {
  if (role === 'super_admin') return true;
  return role === 'admin' && tenancyMode === 'single';
}

/** The values `GET /tags?scope=` accepts. */
export type TagScope = 'all' | TagOwnership;

/** A tag carried by part of a file selection (`GET /tags/for-files`). */
export interface TagOnSelection extends Tag {
  file_count: number;
  selection_size: number;
}

export interface TagShareTarget {
  uuid: string;
  target_type: 'user' | 'group';
  display_name: string;
  shared_by?: string | null;
}

/** Exactly one of the two targets is set. */
export interface TagShareCreate {
  target_user_uuid?: string;
  target_group_uuid?: string;
}

export interface TaggedFile {
  uuid: string;
  display_title: string;
  status?: string | null;
  formatted_duration?: string | null;
}

export interface TagFileList {
  files: TaggedFile[];
  /** Every accessible file carrying the tag, which may exceed `files.length`. */
  total: number;
}

export interface TagListFilters {
  unused?: boolean;
  colliding?: boolean;
  scope?: TagScope;
}

export interface TagClusterMember extends Tag {
  usage_count: number;
  suggested_survivor: boolean;
}

/** A near match of a cluster, never one of its members. */
export interface TagClusterSuggestion extends Tag {
  usage_count: number;
  similarity: number;
}

/** Tags that normalize to the same name. */
export interface TagCollisionCluster {
  normalized_name: string;
  members: TagClusterMember[];
  suggested_survivor_uuid: string | null;
  suggestions: TagClusterSuggestion[];
}

export interface TagImpactEntry {
  uuid: string;
  name: string;
  accessible_file_count: number;
  total_file_count: number;
}

/** What a mutation would touch: the files the caller sees, and all of them. */
export interface TagImpact {
  tags: TagImpactEntry[];
  accessible_file_count: number;
  total_file_count: number;
}

export interface TagRenameRequest {
  name: string;
  confirm_merge?: boolean;
}

export interface TagMergeRequest {
  source_uuids: string[];
}

export interface TagMutationResult {
  tag: Tag | null;
  merged: boolean;
  /** A rename onto another tag's name: nothing was applied, `impact` describes the merge. */
  requires_confirmation: boolean;
  deleted_uuids: string[];
  impact: TagImpact;
}

export type BulkTagAction = 'add_tag' | 'remove_tag';

export type BulkTagOutcome = 'added' | 'already_present' | 'removed' | 'not_present' | 'failed';

export interface BulkTagActionResult {
  file_uuid: string;
  success: boolean;
  message: string;
  error: string | null;
  outcome: BulkTagOutcome | null;
}
