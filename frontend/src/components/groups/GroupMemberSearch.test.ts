/**
 * `GroupMemberSearch.svelte` used to `catch { searchResults = []; }` on every
 * search failure, rendering `groups.noUsersFound` regardless of WHY the
 * request failed. Once `GET /users/search` is rate-limited (issue #904),
 * that reads as "no such user" for a 429 — a lie. The catch block now
 * checks `getErrorStatus(err) === 429` and renders `common.rateLimited`
 * instead.
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

vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

const mockGroupsApi = vi.hoisted(() => ({
  searchUsers: vi.fn(),
  addMember: vi.fn(),
}));
vi.mock('$lib/api/groups', () => ({ GroupsApi: mockGroupsApi }));

import GroupMemberSearch from './GroupMemberSearch.svelte';

beforeEach(() => {
  vi.clearAllMocks();
});

async function typeQuery(getByPlaceholderText: (text: string) => HTMLElement, query: string) {
  const input = getByPlaceholderText('groups.searchUsersPlaceholder') as HTMLInputElement;
  await fireEvent.input(input, { target: { value: query } });
}

describe('GroupMemberSearch — 429 handling (issue #904)', () => {
  it('renders the throttle copy, not "no users found", on a 429', async () => {
    mockGroupsApi.searchUsers.mockRejectedValue({ response: { status: 429 } });

    const { getByPlaceholderText, getByText, queryByText } = render(GroupMemberSearch, {
      props: { groupUuid: 'group-1' },
    });
    await typeQuery(getByPlaceholderText, 'ja');

    await waitFor(() => {
      expect(getByText('common.rateLimited')).toBeTruthy();
    });
    expect(queryByText('groups.noUsersFound')).toBeNull();
  });

  it('renders "no users found" (not the throttle copy) for a genuine empty result set', async () => {
    mockGroupsApi.searchUsers.mockResolvedValue([]);

    const { getByPlaceholderText, getByText, queryByText } = render(GroupMemberSearch, {
      props: { groupUuid: 'group-1' },
    });
    await typeQuery(getByPlaceholderText, 'nobody');

    await waitFor(() => {
      expect(getByText('groups.noUsersFound')).toBeTruthy();
    });
    expect(queryByText('common.rateLimited')).toBeNull();
  });
});
