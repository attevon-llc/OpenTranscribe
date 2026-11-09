/**
 * On a public read-only demo the write controls are disabled and say why. Each test has a
 * not-a-demo control so a "disabled" assertion cannot pass on a control that was never
 * enabled in the first place.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import type { MediaFileDetail } from '$lib/types/media';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);
vi.mock('$stores/supportSession', async () => {
  const { writable } = await import('svelte/store');
  return { supportSessionGate: writable({ active: false, readOnly: false }) };
});
vi.mock('$stores/gallery', async () => {
  const { writable } = await import('svelte/store');
  return {
    galleryStore: new Proxy({}, { get: () => () => {} }),
    galleryState: writable({ isSelecting: false, selectedFiles: new Set<string>() }),
  };
});

import GalleryPrimaryActions from '$components/gallery/GalleryPrimaryActions.svelte';
import FileActionButtons from '$components/fileDetail/FileActionButtons.svelte';
import { demoMode } from '$stores/demo';

const file = {
  uuid: 'f1',
  filename: 'a.mp4',
  upload_time: '2026-01-01T00:00:00Z',
  status: 'completed',
  transcript_segments: [{ uuid: 's1' }],
  has_summary: false,
} as unknown as MediaFileDetail;

beforeEach(() => demoMode.set(false));

describe('Add media', () => {
  it('is enabled outside a demo', () => {
    const { container } = render(GalleryPrimaryActions);

    expect((container.querySelector('.upload-btn') as HTMLButtonElement).disabled).toBe(false);
  });

  it('is disabled in a demo, with the explanation as its tooltip', () => {
    demoMode.set(true);
    const { container } = render(GalleryPrimaryActions);

    const button = container.querySelector('.upload-btn') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('title')).toBe('demo.readOnlyTooltip');
  });
});

describe('summary generation and reprocess', () => {
  const props = { file, canEdit: true, llmAvailable: true };

  it('are enabled outside a demo', () => {
    const { container } = render(FileActionButtons, { props });

    expect((container.querySelector('.generate-summary-btn') as HTMLButtonElement).disabled).toBe(
      false
    );
    expect(
      (container.querySelector('.reprocess-button-header') as HTMLButtonElement).disabled
    ).toBe(false);
  });

  it('are disabled in a demo, with the explanation as their tooltip', () => {
    demoMode.set(true);
    const { container } = render(FileActionButtons, { props });

    for (const selector of ['.generate-summary-btn', '.reprocess-button-header']) {
      const button = container.querySelector(selector) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      expect(button.getAttribute('title')).toBe('demo.readOnlyTooltip');
    }
  });
});
