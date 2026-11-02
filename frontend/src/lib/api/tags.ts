/**
 * HTTP client for the tag endpoints (`/api/tags/*`) and the bulk tag action on
 * the files rail.
 *
 * Transport only: each call returns `response.data` as the server sent it and
 * lets a rejection through untouched, so callers can hand it to
 * `getErrorMessage` / `handleApiError`. No filtering, sorting or counting here;
 * the server owns all of it.
 */
import axiosInstance from '$lib/axios';
import type {
  BulkTagAction,
  BulkTagActionResult,
  Tag,
  TagCollisionCluster,
  TagFileList,
  TagImpact,
  TagListFilters,
  TagMergeRequest,
  TagMutationResult,
  TagOnSelection,
  TagRenameRequest,
  TagShareCreate,
  TagShareTarget,
  TagWithCount,
} from '$lib/types/tag';

/**
 * `list[UUID]` query parameters: the key repeated per value
 * (`tag_uuids=a&tag_uuids=b`), which FastAPI reads, rather than axios' default
 * `tag_uuids[]=` form, which it does not.
 */
function repeatedParam(key: string, values: string[]): URLSearchParams {
  const params = new URLSearchParams();
  for (const value of values) params.append(key, value);
  return params;
}

function tagPath(tagUuid: string): string {
  return `/tags/${tagUuid}`;
}

/** Tags visible to the caller, most used first, then by name. */
export async function listTags(filters: TagListFilters = {}): Promise<TagWithCount[]> {
  const params: Record<string, string | boolean> = {};
  if (filters.unused) params.unused = true;
  if (filters.colliding) params.colliding = true;
  if (filters.scope && filters.scope !== 'all') params.scope = filters.scope;
  const response = await axiosInstance.get('/tags', { params });
  return response.data;
}

/** May return an existing tag the name resolved to rather than a new row. */
export async function createTag(name: string): Promise<Tag> {
  const response = await axiosInstance.post('/tags', { name });
  return response.data;
}

/** Returns the tag actually attached, which may be spelled differently from `name`. */
export async function addTagToFile(fileUuid: string, name: string): Promise<Tag> {
  const response = await axiosInstance.post(`/tags/files/${fileUuid}/tags`, { name });
  return response.data;
}

export async function removeTagFromFile(fileUuid: string, tagName: string): Promise<void> {
  await axiosInstance.delete(`/tags/files/${fileUuid}/tags/${encodeURIComponent(tagName)}`);
}

export async function listFilesForTag(tagUuid: string, limit = 50): Promise<TagFileList> {
  const response = await axiosInstance.get(`${tagPath(tagUuid)}/files`, { params: { limit } });
  return response.data;
}

/** The tags a file selection carries, each with how many of the files have it. */
export async function listTagsOnFiles(fileUuids: string[]): Promise<TagOnSelection[]> {
  const response = await axiosInstance.get('/tags/for-files', {
    params: repeatedParam('file_uuids', fileUuids),
  });
  return response.data;
}

export async function listTagShares(tagUuid: string): Promise<TagShareTarget[]> {
  const response = await axiosInstance.get(`${tagPath(tagUuid)}/shares`);
  return response.data;
}

export async function shareTag(tagUuid: string, payload: TagShareCreate): Promise<TagShareTarget> {
  const response = await axiosInstance.post(`${tagPath(tagUuid)}/shares`, payload);
  return response.data;
}

export async function revokeTagShare(tagUuid: string, shareUuid: string): Promise<void> {
  await axiosInstance.delete(`${tagPath(tagUuid)}/shares/${shareUuid}`);
}

/** Groups of tags whose names normalize alike, with near matches beside each. */
export async function listTagCollisions(): Promise<TagCollisionCluster[]> {
  const response = await axiosInstance.get('/tags/collisions');
  return response.data;
}

/** Files a mutation of these tags would touch, before anything is applied. */
export async function getTagImpact(tagUuids: string[]): Promise<TagImpact> {
  const response = await axiosInstance.get('/tags/impact', {
    params: repeatedParam('tag_uuids', tagUuids),
  });
  return response.data;
}

/**
 * A rename onto another tag's name is a merge. Unless `confirm_merge` is set,
 * the server applies nothing and answers `requires_confirmation` with the impact.
 */
export async function renameTag(
  tagUuid: string,
  payload: TagRenameRequest
): Promise<TagMutationResult> {
  const body: Required<TagRenameRequest> = {
    name: payload.name,
    confirm_merge: payload.confirm_merge ?? false,
  };
  const response = await axiosInstance.patch(tagPath(tagUuid), body);
  return response.data;
}

export async function mergeTags(
  targetUuid: string,
  sourceUuids: string[]
): Promise<TagMutationResult> {
  const body: TagMergeRequest = { source_uuids: sourceUuids };
  const response = await axiosInstance.post(`${tagPath(targetUuid)}/merge`, body);
  return response.data;
}

export async function deleteTags(tagUuids: string[]): Promise<TagMutationResult> {
  const response = await axiosInstance.delete('/tags', {
    params: repeatedParam('tag_uuids', tagUuids),
  });
  return response.data;
}

/** Moves the caller's tags into the shared vocabulary every account sees. */
export async function promoteTags(tagUuids: string[]): Promise<TagMutationResult> {
  const response = await axiosInstance.post('/tags/promote', { tag_uuids: tagUuids });
  return response.data;
}

/** One result per file; a partly failed batch is not a failed request. */
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
