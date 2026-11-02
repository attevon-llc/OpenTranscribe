import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return {
    t: readable((key: string, opts?: Record<string, unknown>) =>
      opts ? `${key} ${JSON.stringify(opts)}` : key
    ),
  };
});

const tagsApi = vi.hoisted(() => ({
  listTags: vi.fn(),
  createTag: vi.fn(),
  bulkTagFiles: vi.fn(),
  listTagsOnFiles: vi.fn(),
}));
vi.mock('$lib/api/tags', () => tagsApi);

import BulkTagModal from './BulkTagModal.svelte';
import type { BulkTagActionResult, BulkTagOutcome, TagOnSelection } from '$lib/types/tag';

const M = 'gallery.bulk.tagModal';
const selection = ['file-a', 'file-b'];

function outcomes(...kinds: (BulkTagOutcome | 'refused')[]): BulkTagActionResult[] {
  return kinds.map((kind, index) => ({
    file_uuid: `file-${index}`,
    success: kind !== 'refused',
    message: kind,
    error: kind === 'refused' ? 'Requires editor permission on this file' : null,
    outcome: kind === 'refused' ? null : kind,
  }));
}

function carried(name: string, file_count = 1, selection_size = 2): TagOnSelection {
  return { uuid: `on-${name}`, name, can_write: true, file_count, selection_size };
}

function pending<V>() {
  let resolve!: (value: V) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<V>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  tagsApi.listTags.mockResolvedValue([
    { uuid: 'l1', name: 'Customer Call', usage_count: 5, can_write: true },
    { uuid: 'l2', name: 'roadmap', usage_count: 2, can_write: true },
    { uuid: 'l3', name: 'legal', usage_count: 1, can_write: true },
  ]);
  tagsApi.listTagsOnFiles.mockResolvedValue([]);
  tagsApi.createTag.mockImplementation(async (name: string) => ({ uuid: 'n', name }));
  tagsApi.bulkTagFiles.mockResolvedValue(outcomes('added', 'added'));
});

const combobox = () => screen.getByRole('combobox') as HTMLInputElement;
const tabNamed = (key: 'addTag' | 'removeTag') =>
  screen.getByRole('tab', { name: `gallery.bulk.${key}` });
const submitButton = () =>
  screen.getByRole('button', { name: new RegExp(`${M}\\.(add|remove)Submit`) });

async function openDialog(
  props: Record<string, unknown> = {},
  events: Record<string, (event: CustomEvent) => void> = {}
) {
  const view = render(BulkTagModal, {
    props: { isOpen: true, action: 'add_tag', fileUuids: selection, ...props },
    events,
  });
  await waitFor(() => expect(tagsApi.listTagsOnFiles).toHaveBeenCalled());
  return view;
}

async function settled() {
  await waitFor(() => expect(document.querySelector('.selection-loading')).toBeNull());
}

describe('the add / remove switch', () => {
  it('names the strip and its two tabs, with the opening direction selected', async () => {
    await openDialog({ action: 'remove_tag' });
    expect(screen.getByRole('tablist', { name: 'gallery.bulk.addOrEditTags' })).toBeInTheDocument();
    expect(tabNamed('addTag')).toHaveAttribute('aria-selected', 'false');
    expect(tabNamed('removeTag')).toHaveAttribute('aria-selected', 'true');
    expect(tabNamed('addTag')).toHaveAttribute('title', 'gallery.bulk.addTagTooltip');
    expect(tabNamed('removeTag')).toHaveAttribute('title', 'gallery.bulk.removeTagTooltip');
  });

  it('clears the result when switched but keeps the typed name', async () => {
    await openDialog();
    await fireEvent.input(combobox(), { target: { value: 'roadmap' } });
    await fireEvent.click(submitButton());
    await screen.findByRole('status');

    await fireEvent.click(tabNamed('removeTag'));
    expect(screen.queryByRole('status')).toBeNull();
    expect(combobox().value).toBe('roadmap');
    expect(submitButton()).toHaveTextContent(`${M}.removeSubmit`);
    expect(screen.getByRole('button', { name: `${M}.cancel` })).toBeInTheDocument();
  });

  it('clears an error when switched', async () => {
    tagsApi.createTag.mockRejectedValue({ response: { data: { detail: 'Tag name is required' } } });
    await openDialog();
    await fireEvent.input(combobox(), { target: { value: '--' } });
    await fireEvent.click(submitButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Tag name is required');

    await fireEvent.click(tabNamed('removeTag'));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('disables both tabs while a batch runs, and the direction cannot change', async () => {
    const batch = pending<BulkTagActionResult[]>();
    tagsApi.bulkTagFiles.mockReturnValue(batch.promise);
    await openDialog();
    await fireEvent.input(combobox(), { target: { value: 'roadmap' } });
    await fireEvent.click(submitButton());
    await waitFor(() => expect(tabNamed('removeTag')).toBeDisabled());
    expect(tabNamed('addTag')).toBeDisabled();

    // A click that reaches the strip anyway must not flip the dialog's direction.
    await fireEvent.click(tabNamed('removeTag'));
    expect(screen.getByRole('heading', { name: `${M}.addTitle {"count":2}` })).toBeInTheDocument();

    batch.resolve(outcomes('added', 'added'));
    await waitFor(() => expect(tabNamed('removeTag')).toBeEnabled());
    expect(await screen.findByRole('status')).toHaveTextContent(
      `${M}.addedCount {"count":2,"name":"roadmap"}`
    );
  });

  it('removes in the switched direction and reports that direction in applied', async () => {
    const applied = vi.fn();
    tagsApi.bulkTagFiles.mockResolvedValue(outcomes('removed', 'not_present'));
    await openDialog({}, { applied });
    await fireEvent.click(tabNamed('removeTag'));
    await fireEvent.input(combobox(), { target: { value: 'ROADMAP' } });
    await fireEvent.click(submitButton());

    await waitFor(() => expect(applied).toHaveBeenCalledTimes(1));
    expect(tagsApi.createTag).not.toHaveBeenCalled();
    expect(tagsApi.bulkTagFiles).toHaveBeenCalledWith(selection, 'remove_tag', 'roadmap');
    expect(applied.mock.calls[0][0].detail).toEqual({
      action: 'remove_tag',
      name: 'roadmap',
      changed: 1,
      unchanged: 1,
      failed: 0,
    });
  });
});

describe('remove suggestions scoped by what the selection carries', () => {
  it('offers only the tags loaded for the selection, with the scoped hint', async () => {
    tagsApi.listTagsOnFiles.mockResolvedValue([carried('legal'), carried('Customer Call', 2)]);
    await openDialog({ action: 'remove_tag' });
    await settled();
    expect(screen.getByText(`${M}.removeHintScoped`)).toBeInTheDocument();

    await fireEvent.input(combobox(), { target: { value: '' } });
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2));
    expect(screen.getAllByRole('option').map((o) => o.textContent?.trim())).toEqual([
      'legal',
      'Customer Call',
    ]);
  });

  it('unions the loaded names with presentTagNames without repeating one', async () => {
    tagsApi.listTagsOnFiles.mockResolvedValue([carried('legal')]);
    await openDialog({ action: 'remove_tag', presentTagNames: ['legal', 'archive'] });
    await settled();
    await fireEvent.input(combobox(), { target: { value: 'a' } });
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2));
    expect(screen.getAllByRole('option').map((o) => o.textContent?.trim())).toEqual([
      'legal',
      'archive',
    ]);
  });

  it('resolves a typed name against a carried tag of another spelling', async () => {
    tagsApi.listTagsOnFiles.mockResolvedValue([carried('Customer Call', 2)]);
    tagsApi.bulkTagFiles.mockResolvedValue(outcomes('removed', 'removed'));
    await openDialog({ action: 'remove_tag' });
    await settled();
    await fireEvent.input(combobox(), { target: { value: 'customer_call' } });
    await fireEvent.click(submitButton());

    const status = await screen.findByRole('status');
    expect(tagsApi.bulkTagFiles).toHaveBeenCalledWith(selection, 'remove_tag', 'Customer Call');
    const lines = Array.from(status.querySelectorAll('p')).map((p) => p.textContent?.trim());
    expect(lines).toEqual([
      `${M}.resolved {"name":"Customer Call","typed":"customer_call"}`,
      `${M}.removedCount {"count":2,"name":"Customer Call"}`,
    ]);
  });

  it('falls back to the whole library and the unscoped hint when the lookup fails', async () => {
    tagsApi.listTagsOnFiles.mockRejectedValue(new Error('lookup failed'));
    await openDialog({ action: 'remove_tag' });
    await settled();
    expect(screen.getByText(`${M}.removeHintAll`)).toBeInTheDocument();
    await fireEvent.input(combobox(), { target: { value: '' } });
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3));
  });

  it('ignores the carried names in add mode', async () => {
    tagsApi.listTagsOnFiles.mockResolvedValue([carried('legal')]);
    await openDialog();
    await settled();
    expect(screen.getByText(`${M}.addHint`)).toBeInTheDocument();
    await fireEvent.input(combobox(), { target: { value: '' } });
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3));
  });
});

describe('outcome counting', () => {
  it('counts a success with a failed outcome and a refused file as failed', async () => {
    const applied = vi.fn();
    tagsApi.bulkTagFiles.mockResolvedValue([
      ...outcomes('added', 'already_present', 'refused'),
      { file_uuid: 'odd', success: true, message: '', error: null, outcome: 'failed' },
    ]);
    await openDialog({ fileUuids: ['1', '2', '3', '4'] }, { applied });
    await fireEvent.input(combobox(), { target: { value: 'legal' } });
    await fireEvent.click(submitButton());

    const status = await screen.findByRole('status');
    expect(applied.mock.calls[0][0].detail).toEqual({
      action: 'add_tag',
      name: 'legal',
      changed: 1,
      unchanged: 1,
      failed: 2,
    });
    expect(status).toHaveTextContent(`${M}.alreadyPresentCount {"count":1}`);
    expect(status).toHaveTextContent(`${M}.failedCount {"count":2}`);
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('removing a chip of a single file', () => {
  it('shows the server detail in an alert and emits no applied when it fails', async () => {
    const applied = vi.fn();
    tagsApi.listTagsOnFiles.mockResolvedValue([carried('legal', 1, 1)]);
    tagsApi.bulkTagFiles.mockRejectedValue({
      response: { status: 404, data: { detail: 'File not found' } },
    });
    await openDialog({ fileUuids: ['only'] }, { applied });
    const chip = await screen.findByRole('button', { name: `${M}.removeChip {"name":"legal"}` });
    await fireEvent.click(chip);

    expect(await screen.findByRole('alert')).toHaveTextContent('File not found');
    expect(applied).not.toHaveBeenCalled();
    expect(
      await screen.findByRole('button', { name: `${M}.removeChip {"name":"legal"}` })
    ).toBeInTheDocument();
  });

  it('falls back to the request-failed key for an error without detail', async () => {
    tagsApi.listTagsOnFiles.mockResolvedValue([carried('legal', 1, 1)]);
    tagsApi.bulkTagFiles.mockRejectedValue({});
    await openDialog({ fileUuids: ['only'] });
    await fireEvent.click(
      await screen.findByRole('button', { name: `${M}.removeChip {"name":"legal"}` })
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(`${M}.requestFailed`);
  });

  it('counts a not_present answer as unchanged in applied', async () => {
    const applied = vi.fn();
    tagsApi.listTagsOnFiles.mockResolvedValue([carried('legal', 1, 1)]);
    tagsApi.bulkTagFiles.mockResolvedValue(outcomes('not_present'));
    await openDialog({ fileUuids: ['only'] }, { applied });
    await fireEvent.click(
      await screen.findByRole('button', { name: `${M}.removeChip {"name":"legal"}` })
    );
    await waitFor(() => expect(applied).toHaveBeenCalledTimes(1));
    expect(applied.mock.calls[0][0].detail).toEqual({
      action: 'remove_tag',
      name: 'legal',
      changed: 0,
      unchanged: 1,
      failed: 0,
    });
  });

  it('clears an earlier error when the next removal starts', async () => {
    tagsApi.listTagsOnFiles.mockResolvedValue([carried('legal', 1, 1)]);
    const second = pending<BulkTagActionResult[]>();
    tagsApi.bulkTagFiles.mockRejectedValueOnce({}).mockReturnValueOnce(second.promise);
    await openDialog({ fileUuids: ['only'] });
    const chip = () => screen.findByRole('button', { name: `${M}.removeChip {"name":"legal"}` });
    await fireEvent.click(await chip());
    await screen.findByRole('alert');

    await fireEvent.click(await chip());
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    second.resolve(outcomes('removed'));
    await waitFor(() => expect(tagsApi.bulkTagFiles).toHaveBeenCalledTimes(2));
  });
});
