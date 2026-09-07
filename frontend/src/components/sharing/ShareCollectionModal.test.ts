/**
 * Regression test for issue #583: `ShareCollectionModal.svelte` used to
 * hardcode `canManage={true}` when rendering `CurrentSharesList`, regardless
 * of whether the current user actually owns the collection being shared.
 * Backend enforcement (`_require_collection_owner`) still blocked the
 * mutating calls server-side, but a non-owner was shown management controls
 * (permission `<select>`, revoke button) that would 403 when used.
 *
 * The fix threads a real `canManage` prop through from the caller
 * (`CollectionsPanel.openShareModal`, derived from
 * `collection.my_permission === 'owner'`) instead of assuming ownership.
 * This test asserts the modal actually renders management controls only
 * when `canManage` is true, by rendering the real (non-mocked)
 * `CurrentSharesList` child — mirroring the gate `CurrentSharesList.test.ts`
 * already pins on that child in isolation.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

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

const mockSharingApi = vi.hoisted(() => ({
  fetchCollectionShares: vi.fn(),
  updateSharePermission: vi.fn(),
  revokeShare: vi.fn(),
  shareCollection: vi.fn(),
}));
vi.mock('$lib/api/sharing', () => ({ SharingApi: mockSharingApi }));

vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

import ShareCollectionModal from './ShareCollectionModal.svelte';
import { sharingStore } from '$stores/sharing';
import type { Share } from '$lib/types/groups';

function makeShare(overrides: Partial<Share> = {}): Share {
  return {
    uuid: 'share-1',
    target_type: 'user',
    target_uuid: 'user-1',
    target_name: 'Alice',
    target_email: 'alice@example.com',
    member_count: null,
    permission: 'viewer',
    shared_by: { uuid: 'owner-1', full_name: 'Owner', email: 'owner@example.com' },
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  sharingStore.reset();
});

describe('ShareCollectionModal — canManage is caller-supplied, never hardcoded', () => {
  it('does not render permission select or revoke button for a non-owner (canManage=false)', async () => {
    mockSharingApi.fetchCollectionShares.mockResolvedValue([makeShare()]);

    const { container, getByText } = render(ShareCollectionModal, {
      props: {
        collectionUuid: 'coll-1',
        collectionName: 'Q3 Planning',
        canManage: false,
      },
    });

    await waitFor(() =>
      expect(mockSharingApi.fetchCollectionShares).toHaveBeenCalledWith('coll-1')
    );
    await waitFor(() => expect(getByText('Alice')).toBeTruthy());

    expect(container.querySelector('select')).toBeNull();
    expect(container.querySelector('.revoke-btn')).toBeNull();
  });

  it('renders permission select and revoke button for the owner (canManage=true)', async () => {
    mockSharingApi.fetchCollectionShares.mockResolvedValue([makeShare({ permission: 'editor' })]);

    const { container, getByText } = render(ShareCollectionModal, {
      props: {
        collectionUuid: 'coll-1',
        collectionName: 'Q3 Planning',
        canManage: true,
      },
    });

    await waitFor(() =>
      expect(mockSharingApi.fetchCollectionShares).toHaveBeenCalledWith('coll-1')
    );
    await waitFor(() => expect(getByText('Alice')).toBeTruthy());

    const select = container.querySelector('select') as HTMLSelectElement;
    expect(select).not.toBeNull();
    expect(select.value).toBe('editor');
    expect(container.querySelector('.revoke-btn')).not.toBeNull();
  });

  it('defaults canManage to false when the caller omits the prop', async () => {
    mockSharingApi.fetchCollectionShares.mockResolvedValue([makeShare()]);

    const { container, getByText } = render(ShareCollectionModal, {
      props: {
        collectionUuid: 'coll-1',
        collectionName: 'Q3 Planning',
      },
    });

    await waitFor(() =>
      expect(mockSharingApi.fetchCollectionShares).toHaveBeenCalledWith('coll-1')
    );
    await waitFor(() => expect(getByText('Alice')).toBeTruthy());

    expect(container.querySelector('select')).toBeNull();
    expect(container.querySelector('.revoke-btn')).toBeNull();
  });
});
