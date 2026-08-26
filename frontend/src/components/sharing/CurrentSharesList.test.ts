/**
 * `CurrentSharesList.svelte` is the child that actually renders the
 * viewer/editor `<select>` and revoke button for each existing share. The
 * `canManage` prop is the ONLY gate: when true it renders `PermissionLevelSelect`
 * + a revoke button per share; when false it renders a read-only permission
 * label instead — no select, no revoke button.
 *
 * IMPORTANT FINDING (see `components/sharing/CLAUDE.md` "Gotchas" + confirmed
 * by reading `ShareCollectionModal.svelte` line 240): the only caller of this
 * component, `ShareCollectionModal.svelte`, passes `canManage={true}`
 * unconditionally — the frontend never actually computes whether the current
 * user owns the collection. So while this component's OWN `canManage` gate is
 * real and tested below, the assumption in the task description ("the control
 * is not rendered when the current user is not the resource owner") does NOT
 * hold anywhere in the app today: every viewer of this modal sees `canManage`
 * management UI regardless of ownership. The security boundary is enforced
 * only by the backend (`_require_collection_owner`), which 403s the mutating
 * calls this component fires. This is a real, pre-existing gap, not a new one
 * introduced here.
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

const mockSharingApi = vi.hoisted(() => ({
  updateSharePermission: vi.fn(),
  revokeShare: vi.fn(),
}));
vi.mock('$lib/api/sharing', () => ({ SharingApi: mockSharingApi }));

vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

import CurrentSharesList from './CurrentSharesList.svelte';
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

describe('CurrentSharesList — canManage gate', () => {
  it('renders no permission select or revoke button when canManage is false', () => {
    const { container, getByText } = render(CurrentSharesList, {
      props: { shares: [makeShare()], canManage: false, collectionUuid: 'coll-1' },
    });

    expect(container.querySelector('select')).toBeNull();
    expect(container.querySelector('.revoke-btn')).toBeNull();
    // Read-only permission label is shown instead.
    expect(getByText('sharing.permissionViewer')).toBeTruthy();
  });

  it('renders the permission select and revoke button when canManage is true', () => {
    const { container } = render(CurrentSharesList, {
      props: {
        shares: [makeShare({ permission: 'editor' })],
        canManage: true,
        collectionUuid: 'coll-1',
      },
    });

    const select = container.querySelector('select') as HTMLSelectElement;
    expect(select.value).toBe('editor');
    expect(container.querySelector('.permission-label')).toBeNull();
    expect(container.querySelector('.revoke-btn')).not.toBeNull();
  });
});

describe('CurrentSharesList — mutating actions call the API with correct args', () => {
  it('changing the permission select calls updateSharePermission with the collection/share ids and updates the store', async () => {
    mockSharingApi.updateSharePermission.mockResolvedValue({});
    const share = makeShare({ uuid: 'share-42', permission: 'viewer' });
    const { container } = render(CurrentSharesList, {
      props: { shares: [share], canManage: true, collectionUuid: 'coll-99' },
    });

    const select = container.querySelector('select') as HTMLSelectElement;
    expect(select).not.toBeNull();
    await fireEvent.change(select, { target: { value: 'editor' } });

    await waitFor(() =>
      expect(mockSharingApi.updateSharePermission).toHaveBeenCalledWith('coll-99', 'share-42', {
        permission: 'editor',
      })
    );

    let shares: Share[] = [];
    sharingStore.subscribe((s) => (shares = s.currentCollectionShares))();
    // updateSharePermission only mutates shares already present in the store.
    expect(shares).toEqual([]);
  });

  it('revoking a share opens a confirmation, then calls revokeShare with the correct ids and removes it from the store', async () => {
    mockSharingApi.revokeShare.mockResolvedValue(undefined);
    const share = makeShare({ uuid: 'share-7' });
    sharingStore.setCurrentShares([share]);
    const { container } = render(CurrentSharesList, {
      props: { shares: [share], canManage: true, collectionUuid: 'coll-5' },
    });

    const revokeBtn = container.querySelector('.revoke-btn') as HTMLElement;
    await fireEvent.click(revokeBtn);

    const confirmBtn = container.querySelector('.modal-delete-button') as HTMLElement;
    await fireEvent.click(confirmBtn);

    await waitFor(() =>
      expect(mockSharingApi.revokeShare).toHaveBeenCalledWith('coll-5', 'share-7')
    );

    let shares: Share[] = [];
    sharingStore.subscribe((s) => (shares = s.currentCollectionShares))();
    expect(shares).toEqual([]);
  });
});
