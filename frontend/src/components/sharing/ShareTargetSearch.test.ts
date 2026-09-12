/**
 * `ShareTargetSearch.svelte` used to `catch { results = []; }` on every
 * search failure, rendering `sharing.noResults` regardless of WHY the
 * request failed. Once `GET /users/search` is rate-limited (issue #904),
 * that reads as "no such person" for a 429 — a lie. The catch block now
 * checks `getErrorStatus(err) === 429` and renders `common.rateLimited`
 * instead.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const mockGroupsApi = vi.hoisted(() => ({
  searchUsers: vi.fn(),
  fetchGroups: vi.fn(),
}));
vi.mock('$lib/api/groups', () => ({ GroupsApi: mockGroupsApi }));

import ShareTargetSearch from './ShareTargetSearch.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  mockGroupsApi.fetchGroups.mockResolvedValue([]);
});

async function typeQuery(getByPlaceholderText: (text: string) => HTMLElement, query: string) {
  const input = getByPlaceholderText('sharing.searchUsersGroups') as HTMLInputElement;
  await fireEvent.input(input, { target: { value: query } });
}

describe('ShareTargetSearch — 429 handling (issue #904)', () => {
  it('renders the throttle copy, not "no results", on a 429', async () => {
    mockGroupsApi.searchUsers.mockRejectedValue({ response: { status: 429 } });

    const { getByPlaceholderText, getByText, queryByText } = render(ShareTargetSearch);
    await typeQuery(getByPlaceholderText, 'ja');

    await waitFor(() => {
      expect(getByText('common.rateLimited')).toBeTruthy();
    });
    expect(queryByText('sharing.noResults')).toBeNull();
  });

  it('renders "no results" (not the throttle copy) for a genuine empty result set', async () => {
    mockGroupsApi.searchUsers.mockResolvedValue([]);

    const { getByPlaceholderText, getByText, queryByText } = render(ShareTargetSearch);
    await typeQuery(getByPlaceholderText, 'nobody');

    await waitFor(() => {
      expect(getByText('sharing.noResults')).toBeTruthy();
    });
    expect(queryByText('common.rateLimited')).toBeNull();
  });
});
