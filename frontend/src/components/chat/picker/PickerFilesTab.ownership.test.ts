/**
 * Fix #4 (W2.0g, the #385 shape recurring in the chat file picker).
 *
 * `PickerFilesTab` listed `GET /files` with no `ownership` param, which the
 * backend defaults to `'mine'` — so a recording shared with the caller but not
 * owned by them never appeared here at all, and chat could never be scoped to
 * it. These tests pin both halves: the request now asks for `ownership: 'all'`
 * (the LEAK-shaped-but-inverted "cannot even select it" failure this closes),
 * and a shared-with-me file is both listed and selectable — the
 * SHARED-VISIBILITY half — while carrying a badge distinguishing it from an
 * owned recording.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

// jsdom has no IntersectionObserver; the infinite-scroll sentinel needs one to
// mount at all (same stub as TranscriptDisplay.test.ts).
class StubIntersectionObserver {
  observe() {}
  disconnect() {}
  unobserve() {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

// Identity translator (same pattern as ChatReasoning.test.ts): returns the raw
// dot-notation key so assertions can check which key was picked without
// booting real i18next + locale JSON in the test environment.
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const auth = vi.hoisted(() => ({ uuid: 'me-uuid' }));

vi.mock('$stores/auth', async () => {
  const { readable } = await import('svelte/store');
  return {
    user: readable({
      get uuid() {
        return auth.uuid;
      },
    }),
  };
});

const axiosMock = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axiosMock }));

import PickerFilesTab from './PickerFilesTab.svelte';

function filesResponse(items: unknown[]) {
  return { data: { items, has_more: false } };
}

describe('PickerFilesTab — ownership (#385 shape, W2.0g fix #4)', () => {
  beforeEach(() => {
    axiosMock.get.mockReset();
    vi.stubGlobal('IntersectionObserver', StubIntersectionObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('requests ownership: all, not the mine-only default', async () => {
    axiosMock.get.mockResolvedValue(filesResponse([]));

    render(PickerFilesTab, { props: { selected: [] } });

    await waitFor(() => expect(axiosMock.get).toHaveBeenCalled());

    const [, options] = axiosMock.get.mock.calls[0];
    expect(options.params.ownership).toBe('all');
  });

  it('lets the caller select a file shared with them, not owned by them', async () => {
    axiosMock.get.mockResolvedValue(
      filesResponse([{ uuid: 'shared-uuid', filename: 'theirs.mp4', user_id: 'other-user-uuid' }])
    );

    const changeHandler = vi.fn();
    render(PickerFilesTab, { props: { selected: [] }, events: { change: changeHandler } });

    const checkbox = await screen.findByTestId('picker-file-checkbox');
    await fireEvent.click(checkbox);

    expect(changeHandler).toHaveBeenCalledTimes(1);
    expect(changeHandler.mock.calls[0][0].detail).toEqual(['shared-uuid']);
  });

  it('badges a shared file as shared, and an owned file gets no badge', async () => {
    axiosMock.get.mockResolvedValue(
      filesResponse([
        { uuid: 'shared-uuid', filename: 'theirs.mp4', user_id: 'other-user-uuid' },
        { uuid: 'owned-uuid', filename: 'mine.mp4', user_id: auth.uuid },
      ])
    );

    render(PickerFilesTab, { props: { selected: [] } });

    await waitFor(() => expect(screen.getAllByTestId('picker-file-checkbox')).toHaveLength(2));

    expect(screen.getByTestId('picker-file-shared-badge')).toBeInTheDocument();
    expect(screen.queryAllByTestId('picker-file-shared-badge')).toHaveLength(1);
  });

  it('shows no badge when every listed file is owned by the caller', async () => {
    axiosMock.get.mockResolvedValue(
      filesResponse([{ uuid: 'owned-uuid', filename: 'mine.mp4', user_id: auth.uuid }])
    );

    render(PickerFilesTab, { props: { selected: [] } });

    await screen.findByTestId('picker-file-checkbox');

    expect(screen.queryByTestId('picker-file-shared-badge')).toBeNull();
  });
});
