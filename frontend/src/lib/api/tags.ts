/**
 * API client for tags — the only place the SPA talks to the `/tags` rail.
 *
 * Shape follows `speakerClusters.ts` (flat exported functions) rather than the
 * `GroupsApi` static-class shape: tags are consumed a call or two at a time by
 * unrelated components (editor, filter sidebar, uploader, watch-source modal),
 * so named imports keep each caller's dependency explicit and tree-shakeable.
 *
 * Transport and types only — filtering, sorting, and counting live on the
 * server (`backend/app/services/tag_collisions.py`), never here.
 */
import axiosInstance from '../axios';
import type {
  BulkTagAction,
  BulkTagActionResult,
  Tag,
  TagCollisionCluster,
  TagFileList,
  TagOnSelection,
  TagShareCreate,
  TagShareTarget,
  TagImpact,
  TagListFilters,
  TagMutationResult,
  TagRenameRequest,
  TagWithCount,
} from '$lib/types/tag';

/**
 * Build the repeated `tag_uuids=…&tag_uuids=…` query FastAPI expects for a
 * `list[UUID] = Query(...)` parameter. Axios' default array serializer emits
 * `tag_uuids[]=…`, which FastAPI rejects as a missing parameter.
 */
function tagUuidParams(tagUuids: string[], extra?: Record<string, string>): URLSearchParams {
  const params = new URLSearchParams();
  for (const uuid of tagUuids) params.append('tag_uuids', uuid);
  for (const [key, value] of Object.entries(extra ?? {})) params.set(key, value);
  return params;
}

/**
 * List tags with usage counts, most used first.
 *
 * The filters are server-side and combine (AND); omit them for the plain list.
 */
export async function listTags(filters: TagListFilters = {}): Promise<TagWithCount[]> {
  const params: Record<string, boolean | string> = {};
  if (filters.unused) params.unused = true;
  if (filters.colliding) params.colliding = true;
  // Ownership is a separate axis from the three view filters, so "my unused
  // tags" is one request rather than an impossible combination.
  if (filters.scope && filters.scope !== 'all') params.scope = filters.scope;
  const response = await axiosInstance.get('/tags', { params });
  return response.data;
}

/** Create a tag (or return the existing one its name resolves to). */
export async function createTag(name: string): Promise<Tag> {
  const response = await axiosInstance.post('/tags', { name });
  return response.data;
}

/** Attach a tag to a file by name, creating the tag if it does not exist. */
export async function addTagToFile(fileUuid: string, name: string): Promise<Tag> {
  const response = await axiosInstance.post(`/tags/files/${fileUuid}/tags`, { name });
  return response.data;
}

/** Detach a tag from a file. The tag row itself survives. */
export async function removeTagFromFile(fileUuid: string, tagName: string): Promise<void> {
  await axiosInstance.delete(`/tags/files/${fileUuid}/tags/${encodeURIComponent(tagName)}`);
}

/**
 * The files a tag is on — what the manager means by "what it touches".
 *
 * Scoped to files the caller can access, so a tag reaching them through one
 * shared file never lists its owner's other media.
 */
export async function listFilesForTag(tagUuid: string, limit = 50): Promise<TagFileList> {
  const response = await axiosInstance.get(`/tags/${tagUuid}/files`, { params: { limit } });
  return response.data;
}

/**
 * The tags a selection of files already carries.
 *
 * `GET /files` carries no per-file tags (#326), so this is the only way the
 * bulk surface can show what the selection has before changing it.
 */
export async function listTagsOnFiles(fileUuids: string[]): Promise<TagOnSelection[]> {
  const params = new URLSearchParams();
  for (const uuid of fileUuids) params.append('file_uuids', uuid);
  const response = await axiosInstance.get('/tags/for-files', { params });
  return response.data;
}

/** Who this tag is shared with. Owner (or admin, for a system tag) only. */
export async function listTagShares(tagUuid: string): Promise<TagShareTarget[]> {
  const response = await axiosInstance.get(`/tags/${tagUuid}/shares`);
  return response.data;
}

/**
 * Share a tag with one user or one group.
 *
 * The middle tier between private and `promoteTags` (which publishes to the
 * whole deployment): the recipient gets the *word* — see, filter, apply — while
 * rename / merge / delete stay with the owner.
 */
export async function shareTag(tagUuid: string, payload: TagShareCreate): Promise<TagShareTarget> {
  const response = await axiosInstance.post(`/tags/${tagUuid}/shares`, payload);
  return response.data;
}

/** Revoke one grant. The tag and every association survive. */
export async function revokeTagShare(tagUuid: string, shareUuid: string): Promise<void> {
  await axiosInstance.delete(`/tags/${tagUuid}/shares/${shareUuid}`);
}

/** Duplicate tags grouped into clusters, each with a preselected survivor. */
export async function listTagCollisions(): Promise<TagCollisionCluster[]> {
  const response = await axiosInstance.get('/tags/collisions');
  return response.data;
}

/**
 * Report what renaming, merging, or deleting these tags would touch, applying
 * nothing. Carries the caller-accessible and global file counts separately.
 */
export async function getTagImpact(tagUuids: string[]): Promise<TagImpact> {
  const response = await axiosInstance.get('/tags/impact', { params: tagUuidParams(tagUuids) });
  return response.data;
}

/**
 * Rename a tag.
 *
 * A new name resolving to a *different* existing tag is a merge: the result
 * comes back with `requires_confirmation` and nothing is applied until the
 * caller retries with `confirm_merge`.
 */
export async function renameTag(
  tagUuid: string,
  payload: TagRenameRequest
): Promise<TagMutationResult> {
  const response = await axiosInstance.patch(`/tags/${tagUuid}`, {
    name: payload.name,
    confirm_merge: payload.confirm_merge ?? false,
  });
  return response.data;
}

/** Fold `sourceUuids` into `targetUuid`, which survives. */
export async function mergeTags(
  targetUuid: string,
  sourceUuids: string[]
): Promise<TagMutationResult> {
  const response = await axiosInstance.post(`/tags/${targetUuid}/merge`, {
    source_uuids: sourceUuids,
  });
  return response.data;
}

/** Delete one or more tags, returning the impact the delete realized. */
export async function deleteTags(tagUuids: string[]): Promise<TagMutationResult> {
  const response = await axiosInstance.delete('/tags', { params: tagUuidParams(tagUuids) });
  return response.data;
}

/**
 * Publish owned tags into the shared vocabulary (admin only).
 *
 * The consolidation lever: a shared tag is visible to every account and is what
 * a typed name resolves onto, so promoting one collapses the private duplicates
 * of that name instead of leaving four "Interview" rows side by side. Their file
 * associations carry over — nobody loses a tag they applied.
 */
export async function promoteTags(tagUuids: string[]): Promise<TagMutationResult> {
  const response = await axiosInstance.post('/tags/promote', { tag_uuids: tagUuids });
  return response.data;
}

/**
 * Attach or detach one tag across many files.
 *
 * Bulk tagging rides the files rail, not `/tags`. Each file reports its own
 * `outcome`, so a partially-applied batch is described rather than failed.
 */
export async function bulkTagFiles(
  fileUuids: string[],
  action: BulkTagAction,
  tagName: string
): Promise<BulkTagActionResult[]> {
  const response = await axiosInstance.post('/files/management/bulk-action', {
    file_uuids: fileUuids,
    action,
    tag_name: tagName,
  });
  return response.data;
}
