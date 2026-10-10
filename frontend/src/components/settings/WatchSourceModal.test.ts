/**
 * A watch source's speaker range is the owner's own override, or nothing (#1198).
 *
 * The modal used to start at 1/20 and always send both numbers, so every watch folder passed a
 * per-file range that beat the owner's saved one. Blank must reach the server as `null`, which
 * it reads as "use my saved range".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
  createWatchSource: vi.fn(),
  updateWatchSource: vi.fn(),
  testWatchSource: vi.fn(),
  browseDirectories: vi.fn(),
  testMultipartRegex: vi.fn(),
}));
vi.mock('$lib/api/watchSourcesApi', () => api);

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn().mockResolvedValue({ data: [] }), post: vi.fn() },
}));
vi.mock('$lib/api/tags', () => ({ listTags: vi.fn().mockResolvedValue([]) }));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import WatchSourceModal from './WatchSourceModal.svelte';
import type { Capabilities } from '$lib/api/watchSourcesApi';

const CAPABILITIES: Capabilities = {
  watch_source_enabled: true,
  local_enabled: true,
  fs_events_enabled: false,
  fs_events_mode: 'auto',
};

function source(overrides: Record<string, unknown> = {}) {
  return {
    uuid: 'ws-1',
    name: 'NAS',
    source_type: 'local',
    local_path: 'inbox',
    polling_interval_minutes: 15,
    use_fs_events: false,
    recursive: true,
    auto_transcribe: true,
    min_speakers: null,
    max_speakers: null,
    multipart_enabled: false,
    multipart_regex: '',
    multipart_time_window_hours: 24,
    multipart_wait_scans: 3,
    upload_stitched_to_source: false,
    tag_names: [],
    collection_ids: [],
    ...overrides,
  };
}

async function openForEdit(editingSource: Record<string, unknown>) {
  render(WatchSourceModal, {
    props: { show: true, editingSource: editingSource as never, capabilities: CAPABILITIES },
  });
  // Edits may jump to any step; the speaker fields are on the "processing" step.
  await waitFor(() => expect(document.querySelectorAll('.step-item').length).toBe(4));
  await fireEvent.click(document.querySelectorAll('.step-item')[1]);
  await waitFor(() => expect(document.querySelector('#ws-min')).not.toBeNull());
}

const min = () => document.querySelector('#ws-min') as HTMLInputElement;
const max = () => document.querySelector('#ws-max') as HTMLInputElement;

async function save() {
  const button = Array.from(document.querySelectorAll<HTMLButtonElement>('.btn-primary')).find(
    (b) => b.textContent?.includes('common.update')
  ) as HTMLButtonElement;
  await fireEvent.click(button);
  await waitFor(() => expect(api.updateWatchSource).toHaveBeenCalled());
  return api.updateWatchSource.mock.calls[0][1] as Record<string, unknown>;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.updateWatchSource.mockResolvedValue(source());
  api.createWatchSource.mockResolvedValue(source());
});

describe('WatchSourceModal speaker range', () => {
  it('shows a source with no range as blank, not 1 and 20', async () => {
    await openForEdit(source());
    expect(min().value).toBe('');
    expect(max().value).toBe('');
    expect(min().placeholder).toBe('settings.watchSources.fields.speakersPlaceholder');
  });

  it("saves blank fields as null so the owner's saved range applies", async () => {
    await openForEdit(source());
    const payload = await save();
    expect(payload.min_speakers).toBeNull();
    expect(payload.max_speakers).toBeNull();
  });

  it('saves a range the owner typed', async () => {
    await openForEdit(source());
    await fireEvent.input(min(), { target: { value: '2' } });
    await fireEvent.input(max(), { target: { value: '6' } });
    const payload = await save();
    expect(payload.min_speakers).toBe(2);
    expect(payload.max_speakers).toBe(6);
  });

  it('keeps an existing range on edit', async () => {
    await openForEdit(source({ min_speakers: 3, max_speakers: 5 }));
    expect(min().value).toBe('3');
    expect(max().value).toBe('5');
  });
});

describe('WatchSourceModal new source', () => {
  it('starts with no range and creates the source with null, not 1 and 20', async () => {
    render(WatchSourceModal, {
      props: { show: true, editingSource: null, capabilities: CAPABILITIES },
    });
    await waitFor(() => expect(document.querySelector('#ws-name')).not.toBeNull());
    await fireEvent.input(document.querySelector('#ws-name') as HTMLInputElement, {
      target: { value: 'New folder' },
    });
    const next = () =>
      Array.from(document.querySelectorAll<HTMLButtonElement>('.btn-primary')).find(
        (b) => b.textContent?.includes('common.next')
      ) as HTMLButtonElement;
    await fireEvent.click(next()); // connection -> processing
    await waitFor(() => expect(document.querySelector('#ws-min')).not.toBeNull());
    expect(min().value).toBe('');
    expect(max().value).toBe('');

    await fireEvent.click(next()); // -> advanced
    await fireEvent.click(next()); // -> organize
    const saveButton = await waitFor(() => {
      const b = Array.from(document.querySelectorAll<HTMLButtonElement>('.btn-primary')).find(
        (x) => x.textContent?.includes('common.save')
      );
      expect(b).toBeDefined();
      return b as HTMLButtonElement;
    });
    await fireEvent.click(saveButton);
    await waitFor(() => expect(api.createWatchSource).toHaveBeenCalled());
    const payload = api.createWatchSource.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.min_speakers).toBeNull();
    expect(payload.max_speakers).toBeNull();
  });
});
