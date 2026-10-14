import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => {
  const t = (key: string) => key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});
const getMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/axios', () => ({ default: { get: getMock } }));

import FileStatusSettings from './FileStatusSettings.svelte';
import { apiCache } from '$lib/apiCache';
import { clearSectionData, setSectionData, SectionDataKey } from '$lib/settings/sectionData';

const payload = (total: number) => ({
  status_counts: { total, completed: total, processing: 0, pending: 0, error: 0 },
  problem_files: { count: 0, files: [] },
  recent_files: { count: 0, files: [] },
  has_problems: false,
});

const counts = (c: HTMLElement) =>
  Array.from(c.querySelectorAll('.count-number')).map((el) => el.textContent?.trim());

beforeEach(() => {
  vi.clearAllMocks();
  clearSectionData();
  apiCache.clear();
});

describe('FileStatusSettings stale-while-revalidate', () => {
  it('shows a cold miss as a spinner, then the data', async () => {
    getMock.mockResolvedValue({ data: payload(3) });
    const { container } = render(FileStatusSettings);
    expect(container.querySelector('.count-number')).toBeNull();
    await waitFor(() => expect(counts(container)[0]).toBe('3'));
  });

  it('paints the remembered summary immediately and swaps in the refreshed one', async () => {
    setSectionData(SectionDataKey.fileStatus, payload(1));
    let resolve!: (v: unknown) => void;
    getMock.mockReturnValue(new Promise((r) => (resolve = r)));
    const { container } = render(FileStatusSettings);

    // Synchronously on screen: the remembered numbers, no spinner.
    expect(counts(container)[0]).toBe('1');
    expect(container.querySelector('.loading-state')).toBeNull();

    resolve({ data: payload(7) });
    await waitFor(() => expect(counts(container)[0]).toBe('7'));
  });

  it('keeps what it showed when the refresh fails', async () => {
    setSectionData(SectionDataKey.fileStatus, payload(2));
    getMock.mockRejectedValue(new Error('offline'));
    const { container } = render(FileStatusSettings);
    await waitFor(() => expect(getMock).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(counts(container)[0]).toBe('2');
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});

describe('Open full screen', () => {
  it('carries the return marker so the full page can offer the way back', () => {
    setSectionData(SectionDataKey.fileStatus, payload(1));
    getMock.mockResolvedValue({ data: payload(1) });
    const { getByTestId } = render(FileStatusSettings);
    expect(getByTestId('file-status-open-full').getAttribute('href')).toBe(
      '/file-status?from=settings&section=file-status'
    );
  });
});
