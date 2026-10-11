/**
 * UX review item 40: row 1 of the gallery toolbar reads Tags, Collections, Add media
 * (Add media rightmost). The classes are e2e-guarded (`test_gallery_actions.py`,
 * `test_tag_management.py`), so this also pins that they survive the reorder.
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/supportSession', async () => {
  const { readable } = await import('svelte/store');
  return { supportSessionGate: readable({ active: false, readOnly: false }) };
});
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((k: string) => k) };
});

import GalleryPrimaryActions from './GalleryPrimaryActions.svelte';
import { galleryStore } from '$stores/gallery';

describe('GalleryPrimaryActions', () => {
  it('orders the buttons Tags, Collections, Add media', () => {
    const { container } = render(GalleryPrimaryActions);
    const order = Array.from(container.querySelectorAll('.gallery-primary-actions > button')).map(
      (b) =>
        (['tags-btn', 'collections-btn', 'upload-btn'] as const).find((c) =>
          b.classList.contains(c)
        )
    );
    expect(order).toEqual(['tags-btn', 'collections-btn', 'upload-btn']);
  });

  it('keeps each e2e-guarded class wired to its own gallery trigger', () => {
    // Real store, not a mock: each button must fire exactly its own trigger.
    const fired: string[] = [];
    const unsubs = [
      galleryStore.onTagsTrigger(() => fired.push('tags')),
      galleryStore.onCollectionsTrigger(() => fired.push('collections')),
      galleryStore.onUploadTrigger(() => fired.push('upload')),
    ];
    const { container } = render(GalleryPrimaryActions);
    (container.querySelector('.tags-btn') as HTMLElement).click();
    expect(fired).toEqual(['tags']);
    (container.querySelector('.collections-btn') as HTMLElement).click();
    expect(fired).toEqual(['tags', 'collections']);
    (container.querySelector('.upload-btn') as HTMLElement).click();
    expect(fired).toEqual(['tags', 'collections', 'upload']);
    unsubs.forEach((u) => u());
  });
});
