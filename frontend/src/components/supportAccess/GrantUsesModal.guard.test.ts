import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

import { listUses } from './GrantUsesModal.testHelpers';

vi.mock('$stores/locale', async () => (await import('./GrantUsesModal.testHelpers')).localeStub);
vi.mock(
  '$lib/api/supportAccess',
  async () => (await import('./GrantUsesModal.testHelpers')).supportAccessStub
);

import GrantUsesModal from './GrantUsesModal.svelte';
import type { SupportGrant } from '$lib/api/supportAccess';

const grantOne = { uuid: 'grant-1' } as SupportGrant;
const grantTwo = { uuid: 'grant-2' } as SupportGrant;

beforeEach(() => {
  vi.clearAllMocks();
  listUses.mockResolvedValue({ items: [], total: 0, server_time: 'x' });
});

describe('GrantUsesModal load guard', () => {
  it('loads once per open grant, not once per render', async () => {
    const { rerender } = render(GrantUsesModal, {
      props: { isOpen: true, grant: grantOne, perspective: 'staff' },
    });
    await waitFor(() => expect(listUses).toHaveBeenCalledTimes(1));
    // Same grant, new object: the guard keys on the uuid.
    await rerender({ grant: { uuid: 'grant-1' } as SupportGrant });
    expect(listUses).toHaveBeenCalledTimes(1);
  });

  it('reloads when a different grant opens and after a close-reopen', async () => {
    const { rerender } = render(GrantUsesModal, {
      props: { isOpen: true, grant: grantOne, perspective: 'staff' },
    });
    await waitFor(() => expect(listUses).toHaveBeenCalledTimes(1));
    await rerender({ grant: grantTwo });
    await waitFor(() => expect(listUses).toHaveBeenCalledTimes(2));
    await rerender({ isOpen: false });
    await rerender({ isOpen: true });
    await waitFor(() => expect(listUses).toHaveBeenCalledTimes(3));
  });
});
