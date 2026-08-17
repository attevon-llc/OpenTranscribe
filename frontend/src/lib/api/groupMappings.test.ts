/**
 * `groupMappings.ts` (`GroupMappingsApi`) is a thin wrapper over
 * `/admin/group-mappings`. The one behavior worth pinning beyond wire shape is
 * that `list()` omits the `source` param entirely when not given, rather than
 * sending it as `undefined` (which axios would otherwise serialize).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('../axios', () => ({ default: mockInstance }));

import { GroupMappingsApi } from './groupMappings';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('list', () => {
  it('omits the source param when not given, and returns every mapping', async () => {
    mockInstance.get.mockResolvedValue({ data: [{ uuid: 'm1' }, { uuid: 'm2' }] });
    const result = await GroupMappingsApi.list();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/group-mappings', { params: undefined });
    expect(result).toHaveLength(2);
  });

  it('narrows by source when given, and returns the mapping list', async () => {
    mockInstance.get.mockResolvedValue({ data: [{ uuid: 'm1', source: 'ldap' }] });
    const result = await GroupMappingsApi.list('ldap');
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/group-mappings', {
      params: { source: 'ldap' },
    });
    expect(result).toHaveLength(1);
  });
});

describe('create / update / remove', () => {
  it('creates a mapping and returns the server row', async () => {
    mockInstance.post.mockResolvedValue({ data: { uuid: 'm1', claim_value: 'engineering' } });
    const result = await GroupMappingsApi.create({ source: 'ldap', claim_value: 'engineering' });
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/group-mappings', {
      source: 'ldap',
      claim_value: 'engineering',
    });
    expect(result.uuid).toBe('m1');
  });

  it('updates a mapping by uuid', async () => {
    mockInstance.put.mockResolvedValue({ data: { uuid: 'm1', grants_role: 'admin' } });
    const result = await GroupMappingsApi.update('m1', { grants_role: 'admin' });
    expect(mockInstance.put).toHaveBeenCalledWith('/admin/group-mappings/m1', {
      grants_role: 'admin',
    });
    expect(result.grants_role).toBe('admin');
  });

  it('removes a mapping by uuid, resolving void rather than swallowing a rejection', async () => {
    await expect(GroupMappingsApi.remove('m1')).resolves.toBeUndefined();
    expect(mockInstance.delete).toHaveBeenCalledWith('/admin/group-mappings/m1');
  });
});

describe('test', () => {
  it('resolves a subject against stored mappings without writing anything', async () => {
    mockInstance.post.mockResolvedValue({
      data: {
        source: 'ldap',
        claim_values: ['engineering'],
        matched_claims: ['engineering'],
        unmatched_claims: [],
        groups: [{ uuid: 'g1', name: 'Engineering' }],
        grants_role: 'user',
        legacy_admin: false,
        effective_role: 'user',
      },
    });

    const result = await GroupMappingsApi.test({ source: 'ldap', claim_values: ['engineering'] });

    expect(mockInstance.post).toHaveBeenCalledWith('/admin/group-mappings/test', {
      source: 'ldap',
      claim_values: ['engineering'],
    });
    expect(result.groups).toEqual([{ uuid: 'g1', name: 'Engineering' }]);
    expect(result.legacy_admin).toBe(false);
  });
});
