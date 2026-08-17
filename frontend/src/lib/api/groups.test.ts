/**
 * `GroupsApi` is a thin static-class CRUD wrapper around `/groups` and
 * `/users/search` — these tests pin request shape and response pass-through.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('../axios', () => ({ default: mockInstance }));

import { GroupsApi } from './groups';

const GROUP_UUID = '11111111-1111-1111-1111-111111111111';
const USER_UUID = '22222222-2222-2222-2222-222222222222';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('GroupsApi', () => {
  it('lists groups', async () => {
    const groups = [{ uuid: GROUP_UUID, name: 'Ops' }];
    mockInstance.get.mockResolvedValue({ data: groups });

    const result = await GroupsApi.fetchGroups();
    expect(mockInstance.get).toHaveBeenCalledWith('/groups');
    expect(result).toEqual(groups);
  });

  it('creates a group', async () => {
    const created = { uuid: GROUP_UUID, name: 'Ops' };
    mockInstance.post.mockResolvedValue({ data: created });

    const result = await GroupsApi.createGroup({ name: 'Ops' });
    expect(mockInstance.post).toHaveBeenCalledWith('/groups', { name: 'Ops' });
    expect(result).toEqual(created);
  });

  it('fetches a group detail by uuid', async () => {
    const detail = { uuid: GROUP_UUID, name: 'Ops', members: [] };
    mockInstance.get.mockResolvedValue({ data: detail });

    const result = await GroupsApi.fetchGroupDetail(GROUP_UUID);
    expect(mockInstance.get).toHaveBeenCalledWith(`/groups/${GROUP_UUID}`);
    expect(result).toEqual(detail);
  });

  it('updates a group', async () => {
    const updated = { uuid: GROUP_UUID, name: 'Ops Team' };
    mockInstance.put.mockResolvedValue({ data: updated });

    const result = await GroupsApi.updateGroup(GROUP_UUID, { name: 'Ops Team' });
    expect(mockInstance.put).toHaveBeenCalledWith(`/groups/${GROUP_UUID}`, { name: 'Ops Team' });
    expect(result).toEqual(updated);
  });

  it('deletes a group', async () => {
    mockInstance.delete.mockResolvedValue({ data: undefined });

    await expect(GroupsApi.deleteGroup(GROUP_UUID)).resolves.toBeUndefined();
    expect(mockInstance.delete).toHaveBeenCalledWith(`/groups/${GROUP_UUID}`);
  });

  it('adds a member', async () => {
    const member = { uuid: 'm1', user_uuid: USER_UUID, role: 'member' };
    mockInstance.post.mockResolvedValue({ data: member });

    const result = await GroupsApi.addMember(GROUP_UUID, { user_uuid: USER_UUID });
    expect(mockInstance.post).toHaveBeenCalledWith(`/groups/${GROUP_UUID}/members`, {
      user_uuid: USER_UUID,
    });
    expect(result).toEqual(member);
  });

  it('updates a member role', async () => {
    const member = { uuid: 'm1', user_uuid: USER_UUID, role: 'admin' };
    mockInstance.put.mockResolvedValue({ data: member });

    const result = await GroupsApi.updateMemberRole(GROUP_UUID, USER_UUID, { role: 'admin' });
    expect(mockInstance.put).toHaveBeenCalledWith(`/groups/${GROUP_UUID}/members/${USER_UUID}`, {
      role: 'admin',
    });
    expect(result).toEqual(member);
  });

  it('removes a member', async () => {
    mockInstance.delete.mockResolvedValue({ data: undefined });

    await expect(GroupsApi.removeMember(GROUP_UUID, USER_UUID)).resolves.toBeUndefined();
    expect(mockInstance.delete).toHaveBeenCalledWith(`/groups/${GROUP_UUID}/members/${USER_UUID}`);
  });

  it('searches users with a query param', async () => {
    const users = [{ uuid: USER_UUID, full_name: 'Jane', email: 'jane@example.com' }];
    mockInstance.get.mockResolvedValue({ data: users });

    const result = await GroupsApi.searchUsers('jane');
    expect(mockInstance.get).toHaveBeenCalledWith('/users/search', { params: { q: 'jane' } });
    expect(result).toEqual(users);
  });
});
