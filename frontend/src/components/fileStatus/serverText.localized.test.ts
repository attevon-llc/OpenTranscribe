/**
 * The API sends English display text next to the raw values (`formatted_file_age` "2 hours ago",
 * `formatted_processing_time`, `display_status` "Completed"). The metadata views must format
 * those from the raw fields in the interface language, falling back to the server text only
 * when the raw field is absent.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    locale: readable('de'),
    t: readable((key: string) => `T:${en[key] ?? key}`),
  };
});
const getMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/axios', () => ({ default: { get: getMock } }));

import FileDetailModal from './FileDetailModal.svelte';
import FileStatusSettings from '$components/settings/FileStatusSettings.svelte';
import { clearSectionData } from '$lib/settings/sectionData';
import { apiCache } from '$lib/apiCache';

const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString();

beforeEach(() => {
  vi.clearAllMocks();
  clearSectionData();
  apiCache.clear();
});

describe('FileDetailModal formats server text in the interface language', () => {
  it('file age, processing time and status come from raw fields, not English strings', () => {
    const { container } = render(FileDetailModal, {
      props: {
        selectedFile: 'f',
        retryingFiles: new Set<string>(),
        detailedStatus: {
          file: {
            uuid: 'f',
            filename: 'a.mp3',
            status: 'completed',
            display_status: 'Completed',
            upload_time: hoursAgo(2),
            formatted_file_age: '2 hours ago',
          },
          can_retry: false,
          is_stuck: false,
          task_details: [
            {
              task_type: 'transcription',
              status: 'completed',
              created_at: '2026-08-01T10:00:00Z',
              completed_at: '2026-08-01T10:01:05Z',
              formatted_processing_time: '1 minute 5 seconds',
            },
          ],
        },
      },
    });
    const text = container.textContent ?? '';
    expect(text).not.toContain('2 hours ago');
    expect(text).toMatch(/vor 2 Stunden/);
    expect(text).not.toContain('1 minute 5 seconds');
    expect(text).toMatch(/1\s?Min\.?\s?5\s?Sek/);
    expect(text).toContain('T:Completed');
  });
});

describe('FileStatusSettings formats server text in the interface language', () => {
  it('shows the file age as a German relative time and a translated status', async () => {
    const file = {
      uuid: 'u1',
      filename: 'x.mp3',
      status: 'error',
      display_status: 'Error',
      formatted_file_age: '3 hours ago',
      upload_time: hoursAgo(3),
    };
    getMock.mockResolvedValue({
      data: {
        status_counts: { total: 1, completed: 0, processing: 0, pending: 0, error: 1 },
        problem_files: { count: 1, files: [file] },
        recent_files: { count: 0, files: [] },
        has_problems: true,
      },
    });
    const { container } = render(FileStatusSettings);
    await waitFor(() => expect(container.querySelector('.file-age')).not.toBeNull());
    expect(container.querySelector('.file-age')?.textContent).toMatch(/vor 3 Stunden/);
    expect(container.textContent).not.toContain('3 hours ago');
  });
});
