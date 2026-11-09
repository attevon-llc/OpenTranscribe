import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

const h = vi.hoisted(() => ({ listUses: vi.fn() }));

vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));
vi.mock('$lib/api/supportAccess', () => ({ SupportAccessApi: { listUses: h.listUses } }));

import GrantUsesModal from './GrantUsesModal.svelte';
import type { SupportGrant } from '$lib/api/supportAccess';

const grantOne = { uuid: 'grant-1' } as SupportGrant;
const grantTwo = { uuid: 'grant-2' } as SupportGrant;

beforeEach(() => {
  vi.clearAllMocks();
  h.listUses.mockResolvedValue({ items: [], total: 0, server_time: 'x' });
});

describe('GrantUsesModal load guard', () => {
  it('loads once per open grant, not once per render', async () => {
    const { rerender } = render(GrantUsesModal, {
      props: { isOpen: true, grant: grantOne, perspective: 'staff' },
    });
    await waitFor(() => expect(h.listUses).toHaveBeenCalledTimes(1));
    // Same grant, new object: the guard keys on the uuid.
    await rerender({ grant: { uuid: 'grant-1' } as SupportGrant });
    expect(h.listUses).toHaveBeenCalledTimes(1);
  });

  it('reloads when a different grant opens and after a close-reopen', async () => {
    const { rerender } = render(GrantUsesModal, {
      props: { isOpen: true, grant: grantOne, perspective: 'staff' },
    });
    await waitFor(() => expect(h.listUses).toHaveBeenCalledTimes(1));
    await rerender({ grant: grantTwo });
    await waitFor(() => expect(h.listUses).toHaveBeenCalledTimes(2));
    await rerender({ isOpen: false });
    await rerender({ isOpen: true });
    await waitFor(() => expect(h.listUses).toHaveBeenCalledTimes(3));
  });
});
