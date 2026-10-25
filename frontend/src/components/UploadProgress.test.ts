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

/**
 * DEFECT THESE CATCH: the status icon was a text glyph from a `switch` whose `default` was the
 * hourglass emoji, so the tray flashed a colour emoji for the queued step of every retry. Every
 * status now maps to a named SVG and carries a translated accessible name.
 */
describe('UploadProgress — status icons are SVG, named, and never a glyph', () => {
  const statuses = [
    ['queued', 'status-queued', 'Queued'],
    ['preparing', 'status-working', 'Preparing'],
    ['uploading', 'upload', 'Uploading'],
    ['processing', 'status-working', 'Processing'],
    ['completed', 'status-success', 'Completed'],
    ['failed', 'status-error', 'Failed'],
    ['cancelled', 'status-cancelled', 'Cancelled'],
  ] as const;

  it.each(statuses)('draws %s as the %s SVG named "%s"', (status, icon, label) => {
    const { container } = render(UploadProgress, { props: { upload: uploadItem({ status }) } });
    const holder = container.querySelector('.upload-icon');

    expect(holder?.getAttribute('role')).toBe('img');
    expect(holder?.getAttribute('aria-label')).toBe(label);
    expect(holder?.querySelector('svg')?.getAttribute('data-icon')).toBe(icon);
    expect(holder?.textContent?.trim()).toBe('');
  });

  it('spins the working icon only while preparing or processing', () => {
    for (const [status] of statuses) {
      const { container, unmount } = render(UploadProgress, {
        props: { upload: uploadItem({ status }) },
      });
      const spinning = container.querySelector('.upload-icon svg')?.classList.contains('spin');
      expect(spinning, status).toBe(status === 'preparing' || status === 'processing');
      unmount();
    }
  });

  it('keeps the progress track on a queued row, so a retry does not collapse and regrow it', () => {
    const { container } = render(UploadProgress, {
      props: { upload: uploadItem({ status: 'queued', progress: 0 }) },
    });
    const track = container.querySelector('.progress-container');
    expect(track?.querySelector('.progress-fill')?.getAttribute('style')).toContain('width: 0%');
    expect(track?.querySelector('.progress-text')?.textContent).toBe('0%');
  });

  it('draws the action buttons and the dedup badge with SVG and names the buttons', () => {
    const { container } = render(UploadProgress, {
      props: { upload: uploadItem({ status: 'failed', dedupSkipped: true }) },
    });
    const retry = container.querySelector('.upload-retry-btn');
    const remove = container.querySelector('.upload-remove-btn');

    expect(retry?.getAttribute('aria-label')).toBe('Retry upload');
    expect(retry?.querySelector('svg')?.getAttribute('data-icon')).toBe('retry');
    expect(remove?.getAttribute('aria-label')).toBe('Remove from list');
    expect(remove?.querySelector('svg')?.getAttribute('data-icon')).toBe('x');
    expect(container.querySelector('.upload-dedup-skipped svg')?.getAttribute('data-icon')).toBe(
      'warning'
    );
    expect(container.textContent).not.toMatch(/[✓✗⊘↑↻⚙⏳⚠]/u);
  });
});
