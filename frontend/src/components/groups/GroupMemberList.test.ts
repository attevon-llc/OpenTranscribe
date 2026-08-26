/**
 * `GroupMemberList.svelte` is the component with the real member-management
 * logic in this folder: `canManageMembers` is derived from the `myRole` prop
 * (`owner` or `admin`), and it gates the role `<select>` + remove button per
 * `components/groups/CLAUDE.md`. As that file's Gotchas note, this gate is
 * COSMETIC — the real enforcement is `_require_group_admin` on the backend —
 * but the derivation itself (`myRole === 'owner' || myRole === 'admin'`) is
 * real client logic worth pinning, and it interacts with a second axis: a
 * plain member who is looking at THEIR OWN row still gets a "leave group"
 * button even though they can't manage others.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string, vars?: Record<string, unknown>) =>
        vars ? `${key}:${JSON.stringify(vars)}` : key
      );
      return () => {};
    },
  },
}));

const mockGroupsApi = vi.hoisted(() => ({
  updateMemberRole: vi.fn(),
  removeMember: vi.fn(),
}));
vi.mock('$lib/api/groups', () => ({ GroupsApi: mockGroupsApi }));

vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return {
    authStore: writable<{ user: { uuid: string } | null }>({ user: { uuid: 'me-uuid' } }),
  };
});

// Svelte 5 removed `component.$on(...)`, so dispatched events are only
// observable through an `on:event` listener in a consumer's markup.
import GroupMemberListTestHost from './GroupMemberListTestHost.svelte';
import { authStore } from '$stores/auth';
import type { GroupMember } from '$lib/types/groups';

function makeMember(overrides: Partial<GroupMember> = {}): GroupMember {
  return {
    uuid: 'member-row-1',
    user_uuid: 'user-1',
    email: 'user1@example.com',
    full_name: 'User One',
    role: 'member',
    joined_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  (authStore as unknown as { set: (v: { user: { uuid: string } | null }) => void }).set({
    user: { uuid: 'me-uuid' },
  });
});

describe('GroupMemberList — canManageMembers gate', () => {
  it('hides the role select and remove button for a plain member viewing someone else', () => {
    const other = makeMember({ user_uuid: 'other-user', role: 'member' });
    const { container } = render(GroupMemberListTestHost, {
      props: { members: [other], groupUuid: 'group-1', myRole: 'member' },
    });

    expect(container.querySelector('.role-select')).toBeNull();
    expect(container.querySelector('.btn-remove')).toBeNull();
  });

  it('shows the role select and remove button for an admin viewing someone else', () => {
    const other = makeMember({ user_uuid: 'other-user', role: 'member' });
    const { container } = render(GroupMemberListTestHost, {
      props: { members: [other], groupUuid: 'group-1', myRole: 'admin' },
    });

    const select = container.querySelector('.role-select') as HTMLSelectElement;
    expect(select.value).toBe('member');
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['admin', 'member']);
    expect(container.querySelector('.btn-remove')).not.toBeNull();
  });

  it('shows a leave-group button (not the manage controls) for a plain member viewing their own row', () => {
    const self = makeMember({ user_uuid: 'me-uuid', role: 'member' });
    const { container } = render(GroupMemberListTestHost, {
      props: { members: [self], groupUuid: 'group-1', myRole: 'member' },
    });

    expect(container.querySelector('.role-select')).toBeNull();
    expect(container.querySelector('.btn-leave')).not.toBeNull();
  });
});

describe('GroupMemberList — mutating actions call the API with correct args', () => {
  it('changing the role select calls updateMemberRole with the group/user ids and the new role, then dispatches roleChanged', async () => {
    mockGroupsApi.updateMemberRole.mockResolvedValue({});
    const member = makeMember({ user_uuid: 'user-42', role: 'member' });
    const dispatched: unknown[] = [];
    const { container } = render(GroupMemberListTestHost, {
      props: {
        members: [member],
        groupUuid: 'group-99',
        myRole: 'owner',
        onRoleChanged: (detail: unknown) => dispatched.push(detail),
      },
    });

    const select = container.querySelector('.role-select') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: 'admin' } });

    await waitFor(() =>
      expect(mockGroupsApi.updateMemberRole).toHaveBeenCalledWith('group-99', 'user-42', {
        role: 'admin',
      })
    );
    expect(dispatched).toEqual([{ userUuid: 'user-42', newRole: 'admin' }]);
  });

  it('removing another member calls removeMember with the correct ids and dispatches memberRemoved (not left)', async () => {
    mockGroupsApi.removeMember.mockResolvedValue(undefined);
    const member = makeMember({ user_uuid: 'user-7' });
    const removed: unknown[] = [];
    const left: unknown[] = [];
    const { container } = render(GroupMemberListTestHost, {
      props: {
        members: [member],
        groupUuid: 'group-5',
        myRole: 'owner',
        onMemberRemoved: (detail: unknown) => removed.push(detail),
        onLeft: () => left.push(true),
      },
    });

    await fireEvent.click(container.querySelector('.btn-remove') as HTMLElement);
    const confirmBtn = container.querySelector('.modal-delete-button') as HTMLElement;
    await fireEvent.click(confirmBtn);

    await waitFor(() =>
      expect(mockGroupsApi.removeMember).toHaveBeenCalledWith('group-5', 'user-7')
    );
    expect(removed).toEqual([{ userUuid: 'user-7' }]);
    expect(left).toEqual([]);
  });

  it('leaving the group (removing your own row) calls removeMember with your own id and dispatches left (not memberRemoved)', async () => {
    mockGroupsApi.removeMember.mockResolvedValue(undefined);
    const self = makeMember({ user_uuid: 'me-uuid', role: 'member' });
    const removed: unknown[] = [];
    const left: unknown[] = [];
    const { container } = render(GroupMemberListTestHost, {
      props: {
        members: [self],
        groupUuid: 'group-5',
        myRole: 'member',
        onMemberRemoved: (detail: unknown) => removed.push(detail),
        onLeft: () => left.push(true),
      },
    });

    await fireEvent.click(container.querySelector('.btn-leave') as HTMLElement);
    const confirmBtn = container.querySelector('.modal-delete-button') as HTMLElement;
    await fireEvent.click(confirmBtn);

    await waitFor(() =>
      expect(mockGroupsApi.removeMember).toHaveBeenCalledWith('group-5', 'me-uuid')
    );
    expect(left).toEqual([true]);
    expect(removed).toEqual([]);
  });
});
