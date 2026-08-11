import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * The client is transport-only, so these tests assert the wire shape each
 * method produces (path, method, params/body) plus the two error paths that
 * the shared helpers own: a cancelled request must stay silent, a server
 * error must surface its FastAPI `detail`.
 */
const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('$lib/axios', async () => {
  const actual = await vi.importActual<typeof import('$lib/axios')>('$lib/axios');
  return { ...actual, default: mockInstance };
});

import {
  addTagToFile,
  bulkTagFiles,
  createTag,
  deleteTags,
  getTagImpact,
  listTagCollisions,
  listTags,
  mergeTags,
  removeTagFromFile,
  renameTag,
} from './tags';
import { handleApiError } from '$lib/utils/apiError';
import { toastStore } from '$stores/toast';

const UUID_A = '11111111-1111-1111-1111-111111111111';
const UUID_B = '22222222-2222-2222-2222-222222222222';

/** Query params reach axios as URLSearchParams for repeated-key endpoints. */
function paramsOf(call: unknown[]): string {
  const config = call[call.length - 1] as { params?: URLSearchParams | Record<string, unknown> };
  const params = config?.params;
  return params instanceof URLSearchParams ? params.toString() : JSON.stringify(params);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: [] });
  mockInstance.post.mockResolvedValue({ data: {} });
  mockInstance.patch.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('tag list + file attachment', () => {
  it('lists tags with no params when unfiltered', async () => {
    mockInstance.get.mockResolvedValue({ data: [{ uuid: UUID_A, name: 'ops', usage_count: 2 }] });
    const tags = await listTags();
    expect(mockInstance.get).toHaveBeenCalledWith('/tags', { params: {} });
    expect(tags).toEqual([{ uuid: UUID_A, name: 'ops', usage_count: 2 }]);
  });

  it('pushes the filters to the server as query params', async () => {
    await listTags({ colliding: true, unused: false });
    expect(mockInstance.get).toHaveBeenCalledWith('/tags', {
      params: { colliding: true },
    });
  });

  it('creates a tag by name', async () => {
    await createTag('Q3 Review');
    expect(mockInstance.post).toHaveBeenCalledWith('/tags', { name: 'Q3 Review' });
  });

  it('attaches a tag to a file by name', async () => {
    await addTagToFile('file-uuid', 'Q3 Review');
    expect(mockInstance.post).toHaveBeenCalledWith('/tags/files/file-uuid/tags', {
      name: 'Q3 Review',
    });
  });

  it('url-encodes the tag name when detaching', async () => {
    await removeTagFromFile('file-uuid', 'a/b c');
    expect(mockInstance.delete).toHaveBeenCalledWith('/tags/files/file-uuid/tags/a%2Fb%20c');
  });
});

describe('collision + unused discovery', () => {
  it('reads collision clusters from the server', async () => {
    await listTagCollisions();
    expect(mockInstance.get).toHaveBeenCalledWith('/tags/collisions');
  });
});

describe('impact previews', () => {
  it('repeats tag_uuids rather than bracket-indexing them', async () => {
    await getTagImpact([UUID_A, UUID_B]);
    expect(mockInstance.get.mock.calls[0][0]).toBe('/tags/impact');
    expect(paramsOf(mockInstance.get.mock.calls[0])).toBe(
      `tag_uuids=${UUID_A}&tag_uuids=${UUID_B}`
    );
  });

  it('carries the accessible and global counts through unchanged', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        tags: [{ uuid: UUID_A, name: 'ops', accessible_file_count: 3, total_file_count: 500 }],
        accessible_file_count: 3,
        total_file_count: 500,
      },
    });
    const impact = await getTagImpact([UUID_A]);
    expect(impact.accessible_file_count).toBe(3);
    expect(impact.total_file_count).toBe(500);
    expect(impact.tags[0].total_file_count).toBe(500);
  });
});

describe('mutations', () => {
  it('renames with an explicit confirm_merge default of false', async () => {
    await renameTag(UUID_A, { name: 'Ops' });
    expect(mockInstance.patch).toHaveBeenCalledWith(`/tags/${UUID_A}`, {
      name: 'Ops',
      confirm_merge: false,
    });
  });

  it('passes confirm_merge through when the caller accepts the merge', async () => {
    await renameTag(UUID_A, { name: 'Ops', confirm_merge: true });
    expect(mockInstance.patch).toHaveBeenCalledWith(`/tags/${UUID_A}`, {
      name: 'Ops',
      confirm_merge: true,
    });
  });

  it('merges sources into the target in the path', async () => {
    await mergeTags(UUID_A, [UUID_B]);
    expect(mockInstance.post).toHaveBeenCalledWith(`/tags/${UUID_A}/merge`, {
      source_uuids: [UUID_B],
    });
  });

  it('deletes via repeated tag_uuids query params', async () => {
    await deleteTags([UUID_A, UUID_B]);
    expect(mockInstance.delete.mock.calls[0][0]).toBe('/tags');
    expect(paramsOf(mockInstance.delete.mock.calls[0])).toBe(
      `tag_uuids=${UUID_A}&tag_uuids=${UUID_B}`
    );
  });
});

describe('bulk tagging', () => {
  it('rides the files rail with a tag_name and returns per-file outcomes', async () => {
    mockInstance.post.mockResolvedValue({
      data: [
        { file_uuid: 'f1', success: true, message: 'ok', error: null, outcome: 'added' },
        {
          file_uuid: 'f2',
          success: true,
          message: 'already',
          error: null,
          outcome: 'already_present',
        },
      ],
    });
    const results = await bulkTagFiles(['f1', 'f2'], 'add_tag', 'Q3 Review');
    expect(mockInstance.post).toHaveBeenCalledWith('/files/management/bulk-action', {
      file_uuids: ['f1', 'f2'],
      action: 'add_tag',
      tag_name: 'Q3 Review',
    });
    expect(results.map((r) => r.outcome)).toEqual(['added', 'already_present']);
  });
});

describe('error surfacing', () => {
  it('does not surface a cancelled request as an error', async () => {
    const cancelled = Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' });
    mockInstance.get.mockRejectedValue(cancelled);
    const spy = vi.spyOn(toastStore, 'error');

    await expect(listTags()).rejects.toBe(cancelled);
    handleApiError(cancelled, 'Failed to load tags');
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it('surfaces a server error through the shared error helper', async () => {
    const serverError = { response: { status: 409, data: { detail: 'Tag name already exists' } } };
    mockInstance.post.mockRejectedValue(serverError);
    const spy = vi.spyOn(toastStore, 'error');

    await expect(createTag('ops')).rejects.toBe(serverError);
    const message = handleApiError(serverError, 'Failed to create tag');
    expect(message).toBe('Tag name already exists');
    expect(spy).toHaveBeenCalledWith('Tag name already exists');
    spy.mockRestore();
  });
});
