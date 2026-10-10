/**
 * Gallery toolbars under a support session (issue #1122): upload / collections / tags
 * creation, chat, export and (for a read grant) every write are not offered, because the
 * backend refuses them. Each test has a no-session control so an "absent" assertion cannot
 * pass on a toolbar that never rendered the control.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

const h = vi.hoisted(() => ({ gate: null as unknown as import('svelte/store').Writable<unknown> }));

vi.mock('$stores/supportSession', async () => {
  const { writable } = await import('svelte/store');
  h.gate = writable({ active: false, readOnly: false });
  return { supportSessionGate: h.gate };
});
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((k: string) => k) };
});
vi.mock('$stores/toast', () => ({ toastStore: { info: vi.fn(), error: vi.fn() } }));
vi.mock('$stores/chat', () => ({ chatStore: { setPendingContext: vi.fn() } }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { user: writable({ role: 'admin' }) };
});
vi.mock('$stores/gallery', async () => {
  const { readable, writable } = await import('svelte/store');
  const noop = () => {};
  return {
    galleryStore: new Proxy({}, { get: () => noop }),
    galleryState: writable({ isSelecting: true, selectedFiles: new Set<string>() }),
    selectedCount: readable(1),
    allFilesSelected: readable(false),
  };
});

import GalleryPrimaryActions from './GalleryPrimaryActions.svelte';
import GallerySelectionActions from './GallerySelectionActions.svelte';
import { galleryState } from '$stores/gallery';
import type { Writable } from 'svelte/store';

const state = galleryState as unknown as Writable<{ isSelecting: boolean }>;
const completed = [{ uuid: 'f1', status: 'completed' }] as never;

beforeEach(() => h.gate.set({ active: false, readOnly: false }));

describe('GalleryPrimaryActions', () => {
  beforeEach(() => state.update((s) => ({ ...s, isSelecting: false })));

  it('normally offers Add media, Collections and Tags', () => {
    render(GalleryPrimaryActions);
    expect(document.querySelector('.upload-btn')?.textContent).toContain('nav.addMedia');
    expect(document.querySelector('.collections-btn')?.textContent).toContain('nav.collections');
    expect(document.querySelector('.tags-btn')?.textContent).toContain('nav.tags');
  });

  it('offers none of them during a support session', () => {
    h.gate.set({ active: true, readOnly: false });
    render(GalleryPrimaryActions);
    expect(document.querySelector('.upload-btn')).toBeNull();
    expect(document.querySelector('.collections-btn')).toBeNull();
    expect(document.querySelector('.tags-btn')).toBeNull();
  });
});

describe('GallerySelectionActions', () => {
  beforeEach(() => state.update((s) => ({ ...s, isSelecting: true })));

  async function openMenus() {
    await fireEvent.click(document.querySelector('.process-btn') as HTMLElement);
    return document;
  }

  it('normally has the Process menu with AI chat, Organize with export, and Delete', async () => {
    render(GallerySelectionActions, { props: { files: completed } });
    expect(document.querySelector('.process-btn')).not.toBeNull();
    expect(document.querySelector('.delete-btn')).not.toBeNull();
    await openMenus();
    expect(document.querySelector('[data-testid="gallery-chat-with-selected"]')).not.toBeNull();
    await fireEvent.click(document.querySelector('.organize-btn') as HTMLElement);
    expect(screen.getByText('gallery.bulk.exportSrt')).toBeTruthy();
  });

  it('a WRITE grant keeps Process but loses chat, collection/tag creation and export', async () => {
    h.gate.set({ active: true, readOnly: false });
    render(GallerySelectionActions, { props: { files: completed } });
    await openMenus();
    expect(document.querySelector('[data-testid="gallery-chat-with-selected"]')).toBeNull();
    await fireEvent.click(document.querySelector('.organize-btn') as HTMLElement);
    expect(screen.queryByText('gallery.bulk.exportSrt')).toBeNull();
    expect(screen.queryByText('gallery.bulk.addToCollection')).toBeNull();
    expect(screen.queryByText('gallery.bulk.addOrEditTags')).toBeNull();
    expect(document.querySelector('.delete-btn')).not.toBeNull();
  });

  it('a READ grant also loses the whole Process menu and Delete', () => {
    h.gate.set({ active: true, readOnly: true });
    render(GallerySelectionActions, { props: { files: completed } });
    expect(document.querySelector('.process-btn')).toBeNull();
    expect(document.querySelector('.delete-btn')).toBeNull();
    expect(document.querySelector('.organize-btn')).not.toBeNull();
  });
});
