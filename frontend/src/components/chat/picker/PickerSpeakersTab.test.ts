/**
 * W2.2: PickerSpeakersTab moved from fetch-everything-then-filter-client-side
 * to server-side type-to-search (`GET /speakers?for_filter=true&q=...`). This
 * pins the request shape (debounced, `q` sent trimmed-or-omitted) and that
 * selection/toggle behaviour is unchanged by the rewrite.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});

vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));

import axiosInstance from '$lib/axios';
import PickerSpeakersTab from './PickerSpeakersTab.svelte';
import PickerSpeakersTabTestHost from './PickerSpeakersTabTestHost.svelte';

const get = vi.mocked(axiosInstance.get);

function speakersResponse(rows: Array<Record<string, unknown>>) {
  return { data: rows };
}

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue(speakersResponse([]));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('PickerSpeakersTab — server-side type-to-search', () => {
  it('fetches on mount with for_filter=true and no q', async () => {
    render(PickerSpeakersTab, { props: { selected: [] } });
    await waitFor(() => expect(get).toHaveBeenCalled());

    const [, config] = get.mock.calls[0];
    expect(config?.params).toMatchObject({ for_filter: true, q: undefined, limit: 100 });
  });

  it('renders rows returned by the server', async () => {
    get.mockResolvedValueOnce(
      speakersResponse([
        { uuid: 'u1', display_name: 'Priya Patel', media_count: 3 },
        { uuid: 'u2', display_name: 'Quinn Zhao', media_count: 1 },
      ])
    );
    const { findByText } = render(PickerSpeakersTab, { props: { selected: [] } });
    expect(await findByText('Priya Patel')).toBeTruthy();
    expect(await findByText('Quinn Zhao')).toBeTruthy();
  });

  it('debounces typed input into a second server request carrying q', async () => {
    const { findByPlaceholderText } = render(PickerSpeakersTab, { props: { selected: [] } });
    const input = await findByPlaceholderText('chat.picker.searchSpeakers');
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    await fireEvent.input(input, { target: { value: 'pri' } });

    // Debounced (300ms) — must not fire immediately.
    expect(get).toHaveBeenCalledTimes(1);

    await waitFor(() => expect(get).toHaveBeenCalledTimes(2), { timeout: 1000 });
    const [, config] = get.mock.calls[1];
    expect(config?.params).toMatchObject({ for_filter: true, q: 'pri' });
  });

  it('a checkbox toggle dispatches change with the display name added', async () => {
    get.mockResolvedValueOnce(
      speakersResponse([{ uuid: 'u1', display_name: 'Priya Patel', media_count: 3 }])
    );
    let changed: string[] | undefined;
    const { findByTestId } = render(PickerSpeakersTabTestHost, {
      props: {
        selected: [],
        onChange: (next: string[]) => {
          changed = next;
        },
      },
    });
    const checkbox = await findByTestId('picker-speaker-checkbox');
    await fireEvent.click(checkbox);
    expect(changed).toEqual(['Priya Patel']);
  });

  it('toggling an already-selected speaker dispatches change with it removed', async () => {
    get.mockResolvedValueOnce(
      speakersResponse([{ uuid: 'u1', display_name: 'Priya Patel', media_count: 3 }])
    );
    let changed: string[] | undefined;
    const { findByTestId } = render(PickerSpeakersTabTestHost, {
      props: {
        selected: ['Priya Patel'],
        onChange: (next: string[]) => {
          changed = next;
        },
      },
    });
    const checkbox = (await findByTestId('picker-speaker-checkbox')) as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    await fireEvent.click(checkbox);
    expect(changed).toEqual([]);
  });
});
