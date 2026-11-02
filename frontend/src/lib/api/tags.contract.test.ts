import { describe, it, expect, vi, beforeEach } from 'vitest';

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

import * as tagsApi from './tags';

const UUID_A = '11111111-1111-1111-1111-111111111111';
const UUID_B = '22222222-2222-2222-2222-222222222222';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: 'GET' });
  mockInstance.post.mockResolvedValue({ data: 'POST' });
  mockInstance.patch.mockResolvedValue({ data: 'PATCH' });
  mockInstance.delete.mockResolvedValue({ data: 'DELETE' });
});

/** The query string a repeated-key call sent, whichever container axios was given. */
function queryOf(call: unknown[]): string {
  const config = call[call.length - 1] as { params: URLSearchParams };
  expect(config.params).toBeInstanceOf(URLSearchParams);
  return config.params.toString();
}

describe('listTags query building', () => {
  it('sends an empty params object, not undefined, when unfiltered', async () => {
    const result = await tagsApi.listTags();
    expect(mockInstance.get).toHaveBeenCalledWith('/tags', { params: {} });
    expect(result).toBe('GET');
  });

  it('omits scope for all and sends it for every other ownership', async () => {
    expect(await tagsApi.listTags({ scope: 'all' })).toBe('GET');
    expect(mockInstance.get).toHaveBeenLastCalledWith('/tags', { params: {} });

    for (const scope of ['mine', 'system', 'shared_with_me'] as const) {
      expect(await tagsApi.listTags({ scope })).toBe('GET');
      expect(mockInstance.get).toHaveBeenLastCalledWith('/tags', { params: { scope } });
    }
  });

  it('combines scope with the unused filter', async () => {
    const result = await tagsApi.listTags({ scope: 'mine', unused: true });
    expect(mockInstance.get).toHaveBeenCalledWith('/tags', {
      params: { unused: true, scope: 'mine' },
    });
    expect(result).toBe('GET');
  });

  it('sends colliding and unused together when both are set', async () => {
    const result = await tagsApi.listTags({ colliding: true, unused: true });
    expect(mockInstance.get).toHaveBeenCalledWith('/tags', {
      params: { unused: true, colliding: true },
    });
    expect(result).toBe('GET');
  });

  it('drops false filters', async () => {
    const result = await tagsApi.listTags({ unused: false, colliding: false });
    expect(mockInstance.get).toHaveBeenCalledWith('/tags', { params: {} });
    expect(result).toBe('GET');
  });
});

describe('request shapes not covered elsewhere', () => {
  it('lists the files of a tag with the default limit of 50', async () => {
    const result = await tagsApi.listFilesForTag(UUID_A);
    expect(mockInstance.get).toHaveBeenCalledWith(`/tags/${UUID_A}/files`, {
      params: { limit: 50 },
    });
    expect(result).toBe('GET');
  });

  it('passes an explicit limit', async () => {
    const result = await tagsApi.listFilesForTag(UUID_A, 7);
    expect(mockInstance.get).toHaveBeenCalledWith(`/tags/${UUID_A}/files`, {
      params: { limit: 7 },
    });
    expect(result).toBe('GET');
  });

  it('repeats file_uuids when listing the tags on a selection', async () => {
    const result = await tagsApi.listTagsOnFiles([UUID_A, UUID_B]);
    expect(mockInstance.get.mock.calls[0][0]).toBe('/tags/for-files');
    expect(queryOf(mockInstance.get.mock.calls[0])).toBe(
      `file_uuids=${UUID_A}&file_uuids=${UUID_B}`
    );
    expect(result).toBe('GET');
  });

  it('posts the uuids to promote in the body, not the query', async () => {
    const result = await tagsApi.promoteTags([UUID_A, UUID_B]);
    expect(mockInstance.post).toHaveBeenCalledWith('/tags/promote', {
      tag_uuids: [UUID_A, UUID_B],
    });
    expect(result).toBe('POST');
  });

  it('reads the shares of a tag', async () => {
    const result = await tagsApi.listTagShares(UUID_A);
    expect(mockInstance.get).toHaveBeenCalledWith(`/tags/${UUID_A}/shares`);
    expect(result).toBe('GET');
  });

  it('creates a share with the payload as the body', async () => {
    const result = await tagsApi.shareTag(UUID_A, { target_user_uuid: UUID_B });
    expect(mockInstance.post).toHaveBeenCalledWith(`/tags/${UUID_A}/shares`, {
      target_user_uuid: UUID_B,
    });
    expect(result).toBe('POST');
  });

  it('revokes a share by tag and share uuid', async () => {
    const result = await tagsApi.revokeTagShare(UUID_A, UUID_B);
    expect(mockInstance.delete).toHaveBeenCalledWith(`/tags/${UUID_A}/shares/${UUID_B}`);
    expect(result).toBeUndefined();
  });

  it('merges into the target named in the path with the sources in the body', async () => {
    const result = await tagsApi.mergeTags(UUID_A, [UUID_B]);
    expect(mockInstance.post).toHaveBeenCalledWith(`/tags/${UUID_A}/merge`, {
      source_uuids: [UUID_B],
    });
    expect(result).toBe('POST');
  });

  it('sends the name in the file attach body and encodes it in the detach path', async () => {
    expect(await tagsApi.addTagToFile('f1', 'a/b')).toBe('POST');
    expect(mockInstance.post).toHaveBeenCalledWith('/tags/files/f1/tags', { name: 'a/b' });
    expect(await tagsApi.removeTagFromFile('f1', 'q3 #1?')).toBeUndefined();
    expect(mockInstance.delete).toHaveBeenCalledWith('/tags/files/f1/tags/q3%20%231%3F');
  });

  it('removes with no return value', async () => {
    await expect(tagsApi.removeTagFromFile('f1', 'x')).resolves.toBeUndefined();
    await expect(tagsApi.revokeTagShare(UUID_A, UUID_B)).resolves.toBeUndefined();
  });

  it('lists collisions with no params', async () => {
    await tagsApi.listTagCollisions();
    expect(mockInstance.get).toHaveBeenCalledTimes(1);
    expect(mockInstance.get.mock.calls[0]).toEqual(['/tags/collisions']);
  });

  it('bulk-removes through the files rail', async () => {
    const result = await tagsApi.bulkTagFiles(['f1'], 'remove_tag', 'x');
    expect(mockInstance.post).toHaveBeenCalledWith('/files/management/bulk-action', {
      file_uuids: ['f1'],
      action: 'remove_tag',
      tag_name: 'x',
    });
    expect(result).toBe('POST');
  });
});

describe('return value and error propagation', () => {
  const calls: Array<[string, 'get' | 'post' | 'patch' | 'delete', () => Promise<unknown>]> = [
    ['listTags', 'get', () => tagsApi.listTags()],
    ['createTag', 'post', () => tagsApi.createTag('x')],
    ['addTagToFile', 'post', () => tagsApi.addTagToFile('f', 'x')],
    ['removeTagFromFile', 'delete', () => tagsApi.removeTagFromFile('f', 'x')],
    ['listFilesForTag', 'get', () => tagsApi.listFilesForTag(UUID_A)],
    ['listTagsOnFiles', 'get', () => tagsApi.listTagsOnFiles([UUID_A])],
    ['listTagShares', 'get', () => tagsApi.listTagShares(UUID_A)],
    ['shareTag', 'post', () => tagsApi.shareTag(UUID_A, { target_group_uuid: UUID_B })],
    ['revokeTagShare', 'delete', () => tagsApi.revokeTagShare(UUID_A, UUID_B)],
    ['listTagCollisions', 'get', () => tagsApi.listTagCollisions()],
    ['getTagImpact', 'get', () => tagsApi.getTagImpact([UUID_A])],
    ['renameTag', 'patch', () => tagsApi.renameTag(UUID_A, { name: 'x' })],
    ['mergeTags', 'post', () => tagsApi.mergeTags(UUID_A, [UUID_B])],
    ['deleteTags', 'delete', () => tagsApi.deleteTags([UUID_A])],
    ['promoteTags', 'post', () => tagsApi.promoteTags([UUID_A])],
    ['bulkTagFiles', 'post', () => tagsApi.bulkTagFiles(['f'], 'add_tag', 'x')],
  ];

  it.each(calls)(
    '%s rejects with the very object the transport rejected with',
    async (_n, verb, call) => {
      const failure = { response: { status: 404, data: { detail: 'Tag not found' } } };
      mockInstance[verb].mockRejectedValue(failure);
      await expect(call()).rejects.toBe(failure);
    }
  );

  it.each(calls.filter(([name]) => !/^(removeTagFromFile|revokeTagShare)$/.test(name)))(
    '%s returns response.data untouched',
    async (_n, verb, call) => {
      const payload = { marker: _n };
      mockInstance[verb].mockResolvedValue({ data: payload });
      await expect(call()).resolves.toBe(payload);
    }
  );

  it('uses exactly the verbs and paths of the contract table', async () => {
    for (const [, , call] of calls) await call();
    const seen = [
      ...mockInstance.get.mock.calls.map((c) => `GET ${c[0]}`),
      ...mockInstance.post.mock.calls.map((c) => `POST ${c[0]}`),
      ...mockInstance.patch.mock.calls.map((c) => `PATCH ${c[0]}`),
      ...mockInstance.delete.mock.calls.map((c) => `DELETE ${c[0]}`),
    ].sort();
    expect(seen).toEqual(
      [
        'GET /tags',
        'POST /tags',
        'POST /tags/files/f/tags',
        `DELETE /tags/files/f/tags/x`,
        `GET /tags/${UUID_A}/files`,
        'GET /tags/for-files',
        `GET /tags/${UUID_A}/shares`,
        `POST /tags/${UUID_A}/shares`,
        `DELETE /tags/${UUID_A}/shares/${UUID_B}`,
        'GET /tags/collisions',
        'GET /tags/impact',
        `PATCH /tags/${UUID_A}`,
        `POST /tags/${UUID_A}/merge`,
        'DELETE /tags',
        'POST /tags/promote',
        'POST /files/management/bulk-action',
      ].sort()
    );
  });
});
