/**
 * DEFECT THESE CATCH: `UploadProgress.svelte` rendered
 * `{upload.estimatedTime} {$t('upload.remaining')}`, but `uploadService`
 * overloaded `estimatedTime` with localized PHASE strings at four sites
 * (`upload.calculatingHash`, `upload.statusUploading`, `upload.resuming`,
 * `upload.statusUploadingExtracted`). Every upload therefore showed
 * "Calculating file hash... remaining" and "Uploading... remaining" before the
 * first real ETA arrived. Nothing failed, because no test named this component.
 *
 * `estimatedTime` is now a duration only; phase text lives in `statusText`.
 *
 * These use the real `en.json` copy — asserting against translation KEYS would
 * not have caught the defect, which is a defect in the rendered SENTENCE.
 */

import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  abortAllRequests: vi.fn(),
  isRequestCancelled: () => false,
}));

import UploadProgress from './UploadProgress.svelte';
import type { UploadItem } from '$lib/services/uploadService';

function uploadItem(overrides: Partial<UploadItem> = {}): UploadItem {
  return {
    id: 'u1',
    type: 'file',
    source: new Blob(['x']),
    name: 'interview.mp4',
    size: 1024 * 1024,
    status: 'uploading',
    progress: 42,
    retryCount: 0,
    ...overrides,
  } as UploadItem;
}

describe('UploadProgress — estimatedTime is a duration, not a status', () => {
  it('renders a real ETA with the "remaining" suffix', () => {
    const { container } = render(UploadProgress, {
      props: { upload: uploadItem({ estimatedTime: '2m 30s' }) },
    });

    expect(container.textContent).toContain('2m 30s remaining');
  });

  it('renders phase text WITHOUT the "remaining" suffix', () => {
    // The exact sentence the bug produced: "Calculating file hash... remaining".
    const { container } = render(UploadProgress, {
      props: {
        upload: uploadItem({ status: 'preparing', statusText: 'Calculating file hash...' }),
      },
    });

    expect(container.textContent).toContain('Calculating file hash...');
    expect(container.textContent).not.toContain('Calculating file hash... remaining');
    expect(container.textContent).not.toMatch(/\.\.\.\s*remaining/);
  });

  it('never suffixes "remaining" onto a non-duration, whatever the phase', () => {
    // All four phase strings uploadService used to write into estimatedTime.
    for (const phase of [
      'Calculating file hash...',
      'Uploading...',
      'Resuming upload...',
      'Uploading extracted audio...',
    ]) {
      const { container, unmount } = render(UploadProgress, {
        props: { upload: uploadItem({ statusText: phase }) },
      });
      expect(container.textContent).toContain(phase);
      expect(container.textContent).not.toContain(`${phase} remaining`);
      unmount();
    }
  });

  it('prefers the ETA once one exists, rather than showing both', () => {
    const { container } = render(UploadProgress, {
      props: { upload: uploadItem({ estimatedTime: '45s', statusText: 'Uploading...' }) },
    });

    expect(container.textContent).toContain('45s remaining');
    expect(container.textContent).not.toContain('Uploading...');
  });

  it('shows neither line before either value exists', () => {
    const { container } = render(UploadProgress, { props: { upload: uploadItem() } });

    expect(container.textContent).not.toContain('remaining');
  });
});
