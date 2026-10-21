import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => {
  const t = (key: string) => key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
const axiosMock = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axiosMock }));

import RetentionSettings from './RetentionSettings.svelte';
import {
  clearSectionData,
  peekSectionData,
  setSectionData,
  SectionDataKey,
} from '$lib/settings/sectionData';

const config = (days: number) => ({
  retention_enabled: false,
  retention_days: days,
  run_time: '02:00',
  timezone: 'UTC',
  delete_error_files: false,
  last_run: null,
  last_run_deleted: null,
});

const days = (c: HTMLElement) =>
  (c.querySelector('#retention-days') as HTMLInputElement | null)?.value;

beforeEach(() => {
  vi.clearAllMocks();
  clearSectionData();
});

describe('RetentionSettings stale-while-revalidate', () => {
  it('shows the skeleton on a cold miss, then the form', async () => {
    axiosMock.get.mockResolvedValue({ data: config(30) });
    const { container } = render(RetentionSettings);
    expect(days(container)).toBeUndefined();
    await waitFor(() => expect(days(container)).toBe('30'));
  });

  it('paints the remembered settings at once and adopts a different server answer', async () => {
    setSectionData(SectionDataKey.retention, config(10));
    let resolve!: (v: unknown) => void;
    axiosMock.get.mockReturnValue(new Promise((r) => (resolve = r)));
    const { container } = render(RetentionSettings);

    await waitFor(() => expect(days(container)).toBe('10'));
    resolve({ data: config(90) });
    await waitFor(() => expect(days(container)).toBe('90'));
  });

  it('never overwrites an edit made while the refresh was in flight', async () => {
    setSectionData(SectionDataKey.retention, config(10));
    let resolve!: (v: unknown) => void;
    axiosMock.get.mockReturnValue(new Promise((r) => (resolve = r)));
    const { container } = render(RetentionSettings);
    await waitFor(() => expect(days(container)).toBe('10'));

    const input = container.querySelector('#retention-days') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: '45' } });
    resolve({ data: config(90) });
    await new Promise((r) => setTimeout(r, 20));

    expect(days(container)).toBe('45');
  });

  it('remembers what a save returned, so the next visit paints it', async () => {
    axiosMock.get.mockResolvedValue({ data: config(30) });
    axiosMock.put.mockResolvedValue({ data: config(60) });
    const { container, getAllByRole } = render(RetentionSettings);
    await waitFor(() => expect(days(container)).toBe('30'));

    await fireEvent.input(container.querySelector('#retention-days') as HTMLInputElement, {
      target: { value: '60' },
    });
    const save = getAllByRole('button').find(
      (b) => b.textContent?.includes('settings.retention.save')
    );
    await fireEvent.click(save as HTMLElement);

    await waitFor(() => expect(axiosMock.put).toHaveBeenCalled());
    await waitFor(() => expect(peekSectionData(SectionDataKey.retention)).toEqual(config(60)));
  });
});
