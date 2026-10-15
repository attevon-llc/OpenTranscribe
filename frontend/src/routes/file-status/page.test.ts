import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';

vi.mock('$stores/locale', () => {
  const t = (key: string) => key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});
// The full status dashboard has its own suite; this file is about the way back to Settings.
vi.mock('$components/UserFileStatus.svelte', async () => ({
  default: (await import('../../test-mocks/SlotHost.svelte')).default,
}));

import Page from './+page.svelte';
import { setPage } from '../../test-mocks/app-stores';
import { gotoCalls } from '../../test-mocks/app-navigation';
import { settingsModalStore } from '$stores/settingsModalStore';
import { stampSettingsReturn } from '$lib/settings/returnToSettings';

beforeEach(() => {
  gotoCalls.length = 0;
  settingsModalStore.reset();
  window.sessionStorage.clear();
  window.history.replaceState({}, '', '/');
});

describe('file-status page: back to Settings', () => {
  it('shows no back control when the page is opened directly', () => {
    setPage('/file-status');
    const { queryByTestId } = render(Page);
    expect(queryByTestId('back-to-settings')).toBeNull();
  });

  it('shows no back control for an unrelated "from"', () => {
    setPage('/file-status?from=notification&section=file-status');
    const { queryByTestId } = render(Page);
    expect(queryByTestId('back-to-settings')).toBeNull();
  });

  it('goes back to the page Settings was over and reopens it on the same section', async () => {
    window.history.replaceState({}, '', '/speakers');
    stampSettingsReturn('file-status');
    setPage('/file-status?from=settings&section=file-status');
    const { getByTestId } = render(Page);

    await fireEvent.click(getByTestId('back-to-settings'));

    await waitFor(() => expect(get(settingsModalStore).isOpen).toBe(true));
    expect(gotoCalls).toEqual(['/speakers']);
    expect(get(settingsModalStore).activeSection).toBe('file-status');
  });

  it('falls back to the gallery when the originating page was not remembered (new tab)', async () => {
    setPage('/file-status?from=settings&section=file-status');
    const { getByTestId } = render(Page);
    await fireEvent.click(getByTestId('back-to-settings'));
    await waitFor(() => expect(get(settingsModalStore).isOpen).toBe(true));
    expect(gotoCalls).toEqual(['/']);
  });
});
