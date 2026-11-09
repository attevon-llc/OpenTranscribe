import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

import { listUses } from './GrantUsesModal.testHelpers';

vi.mock('$stores/locale', async () => (await import('./GrantUsesModal.testHelpers')).localeStub);
vi.mock(
  '$lib/api/supportAccess',
  async () => (await import('./GrantUsesModal.testHelpers')).supportAccessStub
);

import GrantUsesModal from './GrantUsesModal.svelte';
import type { SupportGrant } from '$lib/api/supportAccess';

const grant = { uuid: 'grant-1' } as SupportGrant;

const use = (n: number) => ({
  occurred_at: '2026-10-09T12:00:00Z',
  method: 'GET',
  route: '/api/files/{file_uuid}',
  resource_type: 'media_file',
  resource_uuid: `file-${n}`,
  need: 'read' as const,
});

beforeEach(() => vi.clearAllMocks());

describe('GrantUsesModal', () => {
  it.each(['staff', 'org', 'workspace'] as const)(
    'requests the %s perspective’s uses, 25 at a time',
    async (perspective) => {
      listUses.mockResolvedValue({ items: [use(1)], total: 1, server_time: 'x' });
      render(GrantUsesModal, { props: { isOpen: true, grant, perspective } });
      await waitFor(() => expect(listUses).toHaveBeenCalledTimes(1));
      expect(listUses).toHaveBeenCalledWith(perspective, 'grant-1', { limit: 25, offset: 0 });
    }
  );

  it('renders the route template verbatim, never a concrete path', async () => {
    listUses.mockResolvedValue({ items: [use(1)], total: 1, server_time: 'x' });
    render(GrantUsesModal, { props: { isOpen: true, grant, perspective: 'staff' } });
    expect(await screen.findByText('/api/files/{file_uuid}')).toBeTruthy();
    expect(screen.getByText('file-1')).toBeTruthy();
  });

  it('says "no recorded access" for an empty log and "could not load" for a failure', async () => {
    listUses.mockResolvedValueOnce({ items: [], total: 0, server_time: 'x' });
    const empty = render(GrantUsesModal, { props: { isOpen: true, grant, perspective: 'staff' } });
    expect(await screen.findByText('supportAccess.uses.empty')).toBeTruthy();
    expect(screen.queryByText('supportAccess.uses.loadFailed')).toBeNull();
    empty.unmount();

    listUses.mockRejectedValueOnce(new Error('500'));
    render(GrantUsesModal, { props: { isOpen: true, grant, perspective: 'staff' } });
    expect(await screen.findByText('supportAccess.uses.loadFailed')).toBeTruthy();
    expect(screen.queryByText('supportAccess.uses.empty')).toBeNull();
  });

  it('pages: Load more asks for the next offset and appends', async () => {
    listUses
      .mockResolvedValueOnce({ items: [use(1)], total: 2, server_time: 'x' })
      .mockResolvedValueOnce({ items: [use(2)], total: 2, server_time: 'x' });
    render(GrantUsesModal, { props: { isOpen: true, grant, perspective: 'org' } });
    await fireEvent.click(await screen.findByRole('button', { name: 'supportAccess.loadMore' }));

    await waitFor(() => expect(listUses).toHaveBeenCalledTimes(2));
    expect(listUses).toHaveBeenLastCalledWith('org', 'grant-1', { limit: 25, offset: 1 });
    expect(await screen.findByText('file-2')).toBeTruthy();
    expect(screen.getByText('file-1')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'supportAccess.loadMore' })).toBeNull();
  });
});
