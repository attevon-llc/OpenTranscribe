/**
 * "Place legal hold" in the Organize menu (legal-hold plan, Lane D): offered to the people who
 * may place a hold (instance admins; organization admins with `org.legal_hold` in the managed
 * edition), never to a regular user, enabled once something is selected.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

const h = vi.hoisted(() => ({
  user: null as unknown as import('svelte/store').Writable<Record<string, unknown> | null>,
  caps: null as unknown as import('svelte/store').Writable<Record<string, unknown>>,
}));

vi.mock('$stores/locale', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).englishLocale()
);
vi.mock('$stores/gallery', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).galleryStoreMock()
);
vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  h.user = writable({ role: 'user' });
  return { user: h.user };
});
vi.mock('$stores/capabilities', async () => {
  const { writable } = await import('svelte/store');
  h.caps = writable({ edition: 'cloud', loaded: true, capabilities: {}, audience: {} });
  return {
    capabilities: h.caps,
    isCapabilityEnabled: (s: { capabilities: Record<string, boolean> }, k: string) =>
      s.capabilities[k] !== false,
  };
});
vi.mock('$lib/edition', () => ({ isCloudEdition: true }));

import GallerySelectionActions from './GallerySelectionActions.svelte';

const { galleryTriggers, resetGalleryDouble, selectFiles } = await import(
  '../../test-mocks/galleryToolbarMocks'
);

async function openOrganize() {
  render(GallerySelectionActions, {
    props: { files: [{ uuid: 'a', status: 'completed' }] as never },
  });
  await fireEvent.click(
    screen.getByTitle('Add to collection or export transcripts for selected files')
  );
}

const placeHold = () => screen.queryByRole('button', { name: 'Place legal hold' });

beforeEach(() => {
  resetGalleryDouble();
  h.user.set({ role: 'user' });
  h.caps.set({ edition: 'cloud', loaded: true, capabilities: {}, audience: {} });
});

describe('GallerySelectionActions: Place legal hold', () => {
  it('is not offered to a regular user, while the rest of the menu is', async () => {
    await openOrganize();
    expect(screen.getByRole('button', { name: 'Add to Collection' })).toBeInTheDocument();
    expect(placeHold()).toBeNull();
  });

  it('is offered to an instance admin, disabled with nothing selected', async () => {
    h.user.set({ role: 'admin' });
    await openOrganize();
    expect(placeHold()).toBeDisabled();
  });

  it('is offered to an organization admin with org.legal_hold, and opens the dialog', async () => {
    h.user.set({ role: 'user', org_role: 'org:admin' });
    h.caps.set({
      edition: 'cloud',
      loaded: true,
      capabilities: { 'org.legal_hold': true },
      audience: { 'org.legal_hold': 'org_admin' },
    });
    selectFiles(['a']);
    await openOrganize();
    const entry = placeHold();
    expect(entry).toBeEnabled();
    await fireEvent.click(entry as HTMLElement);
    expect(galleryTriggers.triggerPlaceHold).toHaveBeenCalledTimes(1);
  });

  it('is not offered to an organization member', async () => {
    h.user.set({ role: 'user', org_role: 'org:member' });
    h.caps.set({
      edition: 'cloud',
      loaded: true,
      capabilities: { 'org.legal_hold': true },
      audience: { 'org.legal_hold': 'org_admin' },
    });
    await openOrganize();
    expect(placeHold()).toBeNull();
  });
});
