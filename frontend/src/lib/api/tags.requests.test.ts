import { describe, it, expect, vi, beforeEach } from 'vitest';

const transport = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('$lib/axios', () => ({ default: transport }));

import {
  addTagToFile,
  bulkTagFiles,
  createTag,
  deleteTags,
  getTagImpact,
  listTags,
  removeTagFromFile,
  renameTag,
} from './tags';

const TAG = '0f0f0f0f-1111-2222-3333-444444444444';
const OTHER = '9e9e9e9e-5555-6666-7777-888888888888';
const FILE = 'f00dface-0000-4000-8000-000000000001';

/** The `params` a call handed to the transport, as the query string it becomes. */
function sentQuery(mock: ReturnType<typeof vi.fn>): string {
  const [, config] = mock.mock.calls[0];
  expect(config.params).toBeInstanceOf(URLSearchParams);
  return (config.params as URLSearchParams).toString();
}

beforeEach(() => {
  vi.clearAllMocks();
  for (const verb of ['get', 'post', 'patch', 'delete'] as const) {
    transport[verb].mockResolvedValue({ data: { verb } });
  }
});

describe('request bodies', () => {
  it('creates a tag with just the name in the body', async () => {
    await createTag('Q3 Earnings');
    expect(transport.post).toHaveBeenCalledTimes(1);
    expect(transport.post).toHaveBeenCalledWith('/tags', { name: 'Q3 Earnings' });
  });

  it('attaches a tag under the file uuid with the name in the body, not the path', async () => {
    await addTagToFile(FILE, 'needs review');
    expect(transport.post).toHaveBeenCalledWith(`/tags/files/${FILE}/tags`, {
      name: 'needs review',
    });
  });

  it('sends confirm_merge false explicitly when a rename omits it', async () => {
    await renameTag(TAG, { name: 'Roadmap' });
    expect(transport.patch).toHaveBeenCalledTimes(1);
    const [path, body] = transport.patch.mock.calls[0];
    expect(path).toBe(`/tags/${TAG}`);
    expect(body).toStrictEqual({ name: 'Roadmap', confirm_merge: false });
  });

  it('passes confirm_merge true through when the caller confirms the merge', async () => {
    await renameTag(TAG, { name: 'Roadmap', confirm_merge: true });
    expect(transport.patch.mock.calls[0][1]).toStrictEqual({
      name: 'Roadmap',
      confirm_merge: true,
    });
  });

  it('applies a bulk add through the files rail with the action and the tag name', async () => {
    await bulkTagFiles([FILE, 'f2'], 'add_tag', 'Interview');
    expect(transport.post).toHaveBeenCalledWith('/files/management/bulk-action', {
      file_uuids: [FILE, 'f2'],
      action: 'add_tag',
      tag_name: 'Interview',
    });
  });
});

describe('repeated-key query parameters', () => {
  it('asks for the impact with tag_uuids repeated per tag', async () => {
    await getTagImpact([TAG, OTHER]);
    expect(transport.get.mock.calls[0][0]).toBe('/tags/impact');
    expect(sentQuery(transport.get)).toBe(`tag_uuids=${TAG}&tag_uuids=${OTHER}`);
  });

  it('deletes with tag_uuids repeated per tag and no body', async () => {
    await deleteTags([TAG, OTHER]);
    expect(transport.delete.mock.calls[0][0]).toBe('/tags');
    expect(sentQuery(transport.delete)).toBe(`tag_uuids=${TAG}&tag_uuids=${OTHER}`);
    expect(Object.keys(transport.delete.mock.calls[0][1])).toEqual(['params']);
  });

  it('sends a single uuid once, with no bracket suffix', async () => {
    await getTagImpact([TAG]);
    expect(sentQuery(transport.get)).toBe(`tag_uuids=${TAG}`);
  });
});

describe('listTags filters', () => {
  it('sends shared_with_me as the scope value unchanged', async () => {
    await listTags({ scope: 'shared_with_me' });
    expect(transport.get).toHaveBeenCalledWith('/tags', { params: { scope: 'shared_with_me' } });
  });

  it('sends all three filters together when unused, colliding and a scope are set', async () => {
    await listTags({ unused: true, colliding: true, scope: 'system' });
    expect(transport.get).toHaveBeenCalledWith('/tags', {
      params: { unused: true, colliding: true, scope: 'system' },
    });
  });
});

describe('results and failures', () => {
  it('returns the server list as the same array, in server order', async () => {
    const rows = [
      { uuid: OTHER, name: 'zulu', usage_count: 9, can_write: true },
      { uuid: TAG, name: 'alpha', usage_count: 1, can_write: true },
    ];
    transport.get.mockResolvedValue({ data: rows });
    const listed = await listTags();
    expect(listed).toBe(rows);
    expect(listed.map((row) => row.name)).toEqual(['zulu', 'alpha']);
  });

  it('lets an Error thrown by the transport through as the same instance', async () => {
    const failure = Object.assign(new Error('Network Error'), { code: 'ERR_NETWORK' });
    transport.post.mockRejectedValue(failure);
    await expect(addTagToFile(FILE, 'x')).rejects.toBe(failure);
  });

  it('lets a 422 rejection of a detach through untouched', async () => {
    const failure = { response: { status: 422, data: { detail: 'Tag name is required' } } };
    transport.delete.mockRejectedValue(failure);
    await expect(removeTagFromFile(FILE, '---')).rejects.toBe(failure);
    expect(transport.delete).toHaveBeenCalledWith(`/tags/files/${FILE}/tags/---`);
  });
});
