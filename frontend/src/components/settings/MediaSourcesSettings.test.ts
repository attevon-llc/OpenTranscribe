/**
 * A media source configured for plain http is flagged: a warning while it is typed into the
 * form, and a badge on the saved source, because its credentials cross the network unencrypted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { tick } from 'svelte';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockGetMediaSources = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/mediaSourcesApi', () => ({
  getMediaSources: mockGetMediaSources,
  addMediaSource: vi.fn(),
  updateMediaSource: vi.fn(),
  deleteMediaSource: vi.fn(),
  toggleMediaSourceShare: vi.fn(),
}));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import MediaSourcesSettings from './MediaSourcesSettings.svelte';

function source(uuid: string, hostname: string, isOwn: boolean) {
  return {
    uuid,
    hostname,
    provider_type: 'mediacms',
    username: '',
    has_credentials: true,
    verify_ssl: true,
    label: '',
    is_active: true,
    is_shared: !isOwn,
    is_own: isOwn,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetMediaSources.mockResolvedValue({
    sources: [
      source('a', 'http://intranet.example.com:8080', true),
      source('b', 'https://media.example.com', true),
    ],
    shared_sources: [source('c', 'http://shared.example.com', false)],
  });
});

describe('plain-http media sources', () => {
  it('badges every saved source that uses plain http, and only those', async () => {
    const { container } = render(MediaSourcesSettings);
    await waitFor(() => expect(container.querySelectorAll('.source-card').length).toBe(3));

    const badged = [...container.querySelectorAll('.source-card')]
      .filter((card) => card.querySelector('.meta-badge.http-plain'))
      .map((card) => card.querySelector('.source-hostname')?.textContent?.trim());
    expect(badged).toEqual([
      expect.stringContaining('http://intranet.example.com:8080'),
      expect.stringContaining('http://shared.example.com'),
    ]);
  });

  it('warns while an http:// address is typed into the add form', async () => {
    const { container, getByText } = render(MediaSourcesSettings);
    await waitFor(() => getByText('settings.mediaSources.addSource'));
    await fireEvent.click(getByText('settings.mediaSources.addSource'));
    await tick();

    const input = container.querySelector('#new-hostname') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'http://intranet.example.com' } });
    const warning = container.querySelector('.http-warning');
    expect(warning?.textContent).toBe('settings.mediaSources.plainHttpWarning');
    expect(warning?.getAttribute('role')).toBe('alert');

    await fireEvent.input(input, { target: { value: 'intranet.example.com' } });
    expect(container.querySelector('.http-warning')).toBeNull();
    await fireEvent.input(input, { target: { value: 'https://intranet.example.com' } });
    expect(container.querySelector('.http-warning')).toBeNull();
  });

  it('warns in the edit form for a source saved as http://', async () => {
    const { container, getAllByText } = render(MediaSourcesSettings);
    await waitFor(() => expect(container.querySelectorAll('.source-card').length).toBe(3));

    await fireEvent.click(getAllByText('common.edit')[0]);
    await tick();

    expect(container.querySelector('#edit-hostname')).not.toBeNull();
    expect(container.querySelector('.http-warning')).not.toBeNull();
  });
});
