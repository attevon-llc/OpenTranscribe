import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyWithOptionsLocale()
);

const api = vi.hoisted(() => ({
  listTags: vi.fn(),
  createTag: vi.fn(),
  bulkTagFiles: vi.fn(),
  listTagsOnFiles: vi.fn(),
}));
vi.mock('$lib/api/tags', () => api);

import BulkTagModal from './BulkTagModal.svelte';
import type { BulkTagActionResult, BulkTagOutcome } from '$lib/types/tag';

const T = 'gallery.bulk.tagModal';

const files = ['f1', 'f2', 'f3'];

function results(outcomes: BulkTagOutcome[]): BulkTagActionResult[] {
  return outcomes.map((outcome, i) => ({
    file_uuid: `f${i + 1}`,
    success: outcome !== 'failed',
    message: outcome,
    error: outcome === 'failed' ? 'E' : null,
    outcome,
  }));
}

function deferred<V>() {
  let resolve!: (value: V) => void;
  const promise = new Promise<V>((res) => (resolve = res));
  return { promise, resolve };
}

const libraryNames = ['interview', 'roadmap', 'q3 review'];

beforeEach(() => {
  vi.clearAllMocks();
  api.listTags.mockResolvedValue(libraryNames.map((name, i) => ({ uuid: `t${i}`, name })));
  api.listTagsOnFiles.mockResolvedValue([]);
  api.createTag.mockImplementation(async (name: string) => ({ uuid: 'x', name }));
  api.bulkTagFiles.mockResolvedValue(results(['added', 'added', 'added']));
});

const field = () => screen.getByRole('combobox') as HTMLInputElement;
const primary = (action: 'add' | 'remove' = 'add') =>
  screen.getByRole('button', { name: `${T}.${action}Submit` });
const typeName = (value: string) => fireEvent.input(field(), { target: { value } });

async function open(
  props: Record<string, unknown> = {},
  events: Record<string, (event: CustomEvent) => void> = {}
) {
  const view = render(BulkTagModal, {
    props: { isOpen: true, action: 'add_tag', fileUuids: files, ...props },
    events,
  });
  await waitFor(() => expect(api.listTags).toHaveBeenCalled());
  return view;
}

async function submitAdd(name: string) {
  await typeName(name);
  await fireEvent.click(primary());
}

describe('opening', () => {
  it('does nothing while closed', async () => {
    render(BulkTagModal, { props: { isOpen: false, fileUuids: files } });
    await Promise.resolve();
    expect(api.listTags).not.toHaveBeenCalled();
    expect(api.listTagsOnFiles).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('titles the dialog by mode with the file count and loads names and selection tags', async () => {
    const view = await open();
    expect(screen.getByRole('heading', { name: `${T}.addTitle {"count":3}` })).toBeInTheDocument();
    expect(api.listTagsOnFiles).toHaveBeenCalledWith(files);

    // D14 (6fbe1847e): `action` only seeds the direction when the dialog opens; changing it
    // on an open dialog is ignored, and the user switches with the Remove tab instead.
    await view.rerender({ isOpen: true, action: 'remove_tag', fileUuids: files });
    expect(screen.getByRole('heading', { name: `${T}.addTitle {"count":3}` })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: `${T}.removeTitle {"count":3}` })).toBeNull();

    await fireEvent.click(screen.getByRole('tab', { name: 'gallery.bulk.removeTag' }));
    expect(
      screen.getByRole('heading', { name: `${T}.removeTitle {"count":3}` })
    ).toBeInTheDocument();

    // Reopening re-seeds the direction from `action`.
    await view.rerender({ isOpen: false, action: 'add_tag', fileUuids: files });
    await view.rerender({ isOpen: true, action: 'add_tag', fileUuids: files });
    expect(screen.getByRole('heading', { name: `${T}.addTitle {"count":3}` })).toBeInTheDocument();
    await view.rerender({ isOpen: false, action: 'remove_tag', fileUuids: files });
    await view.rerender({ isOpen: true, action: 'remove_tag', fileUuids: files });
    expect(
      screen.getByRole('heading', { name: `${T}.removeTitle {"count":3}` })
    ).toBeInTheDocument();
  });

  it('labels the field and picks the placeholder and hint by mode', async () => {
    const view = await open();
    expect(screen.getByLabelText(`${T}.label`)).toBe(field());
    expect(field()).toHaveAttribute('placeholder', `${T}.addPlaceholder`);
    expect(screen.getByText(`${T}.addHint`)).toBeInTheDocument();

    // D14 (6fbe1847e): the direction is the dialog's own state (the Remove tab), not the prop.
    await fireEvent.click(screen.getByRole('tab', { name: 'gallery.bulk.removeTag' }));
    expect(field()).toHaveAttribute('placeholder', `${T}.removePlaceholder`);
    expect(screen.getByText(`${T}.removeHintAll`)).toBeInTheDocument();
    expect(screen.queryByText(`${T}.addHint`)).toBeNull();

    await view.rerender({ isOpen: false, fileUuids: files, presentTagNames: ['roadmap'] });
    await view.rerender({
      isOpen: true,
      action: 'remove_tag',
      fileUuids: files,
      presentTagNames: ['roadmap'],
    });
    expect(field()).toHaveAttribute('placeholder', `${T}.removePlaceholder`);
    expect(screen.getByText(`${T}.removeHintScoped`)).toBeInTheDocument();
  });

  it('resets the field, result and error each time it reopens and reloads the names', async () => {
    const view = await open();
    await submitAdd('interview');
    await screen.findByRole('status');

    await view.rerender({ isOpen: false, fileUuids: files });
    await view.rerender({ isOpen: true, fileUuids: files });
    await waitFor(() => expect(api.listTags).toHaveBeenCalledTimes(2));
    expect(field().value).toBe('');
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('survives the name list failing to load and still lets a name be applied', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    api.listTags.mockRejectedValue(new Error('x'));
    await open();
    await submitAdd('roadmap');
    await waitFor(() => expect(api.bulkTagFiles).toHaveBeenCalledWith(files, 'add_tag', 'roadmap'));
    spy.mockRestore();
  });
});

describe('suggestions', () => {
  it('matches the normalized query as a substring of the normalized names', async () => {
    await open({ action: 'remove_tag' });
    await typeName('Q3_REV');
    expect(await screen.findByRole('option', { name: 'q3 review' })).toBeInTheDocument();
    expect(screen.getAllByRole('option')).toHaveLength(1);
  });

  it('offers every name when the query normalizes to nothing', async () => {
    await open({ action: 'remove_tag' });
    await typeName('--');
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3));
  });

  it('shows the empty text when nothing matches', async () => {
    await open({ action: 'remove_tag' });
    await typeName('zzz');
    expect(await screen.findByText(`${T}.noSuggestions`)).toBeInTheDocument();
  });

  it('caps the list at 25', async () => {
    api.listTags.mockResolvedValue(
      Array.from({ length: 30 }, (_, i) => ({
        uuid: `t${i}`,
        name: `tag ${String(i).padStart(2, '0')}`,
      }))
    );
    await open({ action: 'remove_tag' });
    await typeName('tag');
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(25));
  });

  it('scopes remove suggestions to the de-duplicated present names, dropping blanks', async () => {
    await open({
      action: 'remove_tag',
      presentTagNames: ['roadmap', 'roadmap', '  ', 'q3 review'],
    });
    await fireEvent.input(field(), { target: { value: 'r' } });
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2));
    expect(screen.getAllByRole('option').map((o) => o.textContent?.trim())).toEqual([
      'roadmap',
      'q3 review',
    ]);
  });

  it('ignores presentTagNames in add mode', async () => {
    await open({ action: 'add_tag', presentTagNames: ['roadmap'] });
    await typeName('interview');
    expect(await screen.findByRole('option', { name: 'interview' })).toBeInTheDocument();
  });
});

describe('submit gating', () => {
  it('needs a non-blank name', async () => {
    await open();
    expect(primary()).toBeDisabled();
    await typeName('   ');
    expect(primary()).toBeDisabled();
    await typeName(' a ');
    expect(primary()).toBeEnabled();
  });

  it('needs at least one file', async () => {
    await open({ fileUuids: [] });
    await typeName('a');
    expect(primary()).toBeDisabled();
    expect(api.listTagsOnFiles).not.toHaveBeenCalled();
  });

  it('also submits from the form', async () => {
    await open();
    await typeName('interview');
    await fireEvent.submit(field().closest('form') as HTMLFormElement);
    await waitFor(() => expect(api.bulkTagFiles).toHaveBeenCalledTimes(1));
  });
});

describe('adding', () => {
  it('resolves the trimmed name through createTag and applies the resolved name', async () => {
    api.createTag.mockResolvedValue({ uuid: 'a', name: 'interview' });
    const applied = vi.fn();
    await open({}, { applied });
    await submitAdd('  Interview ');

    await waitFor(() =>
      expect(api.bulkTagFiles).toHaveBeenCalledWith(files, 'add_tag', 'interview')
    );
    expect(api.createTag).toHaveBeenCalledWith('Interview');
    expect(applied.mock.calls.map((c) => c[0].detail)).toEqual([
      { action: 'add_tag', name: 'interview', changed: 3, unchanged: 0, failed: 0 },
    ]);
  });

  it('states the resolution before the counts, only when the name differs', async () => {
    api.createTag.mockResolvedValue({ uuid: 'a', name: 'interview' });
    await open();
    await submitAdd('Interview');

    const summary = await screen.findByRole('status');
    expect(summary).toHaveAttribute('aria-live', 'polite');
    expect(summary.classList.contains('result')).toBe(true);
    const text = summary.textContent!;
    const resolved = `${T}.resolved {"name":"interview","typed":"Interview"}`;
    const added = `${T}.addedCount {"count":3,"name":"interview"}`;
    expect(text).toContain(resolved);
    expect(text.indexOf(resolved)).toBeLessThan(text.indexOf(added));
    expect(within(summary).getByRole('heading')).toHaveTextContent(`${T}.resultTitle`);
  });

  it('omits the resolution line when the applied name is the typed one', async () => {
    await open();
    await submitAdd('roadmap');
    const summary = await screen.findByRole('status');
    expect(summary).not.toHaveTextContent('.resolved');
  });

  it('always shows the added line, even at zero, and the other lines only when non-zero', async () => {
    api.bulkTagFiles.mockResolvedValue(
      results(['already_present', 'already_present', 'already_present'])
    );
    await open();
    await submitAdd('roadmap');
    const summary = await screen.findByRole('status');
    expect(summary).toHaveTextContent(`${T}.addedCount {"count":0,"name":"roadmap"}`);
    expect(summary).toHaveTextContent(`${T}.alreadyPresentCount {"count":3}`);
    expect(summary).not.toHaveTextContent('failedCount');
  });

  it('counts success false and a failed outcome as failed without raising an alert', async () => {
    api.bulkTagFiles.mockResolvedValue([
      { file_uuid: 'f1', success: false, message: '', error: 'E', outcome: 'added' },
      { file_uuid: 'f2', success: true, message: '', error: null, outcome: 'failed' },
      { file_uuid: 'f3', success: true, message: '', error: null, outcome: 'added' },
    ]);
    const applied = vi.fn();
    await open({}, { applied });
    await submitAdd('roadmap');
    const summary = await screen.findByRole('status');
    expect(summary).toHaveTextContent(`${T}.failedCount {"count":2}`);
    expect(summary).toHaveTextContent(`${T}.addedCount {"count":1,"name":"roadmap"}`);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(applied.mock.calls[0][0].detail).toMatchObject({ changed: 1, unchanged: 0, failed: 2 });
  });

  it('shows the server detail and applies nothing when resolving the name fails', async () => {
    api.createTag.mockRejectedValue({ response: { data: { detail: 'Tag name is required' } } });
    const applied = vi.fn();
    await open({}, { applied });
    await submitAdd('roadmap');

    expect(await screen.findByRole('alert')).toHaveTextContent('Tag name is required');
    expect(api.bulkTagFiles).not.toHaveBeenCalled();
    expect(screen.queryByRole('status')).toBeNull();
    expect(applied).not.toHaveBeenCalled();
    expect(primary()).toBeEnabled();
  });

  it('falls back to the request-failed key when the error has no detail', async () => {
    api.bulkTagFiles.mockRejectedValue({});
    await open();
    await submitAdd('roadmap');
    expect(await screen.findByRole('alert')).toHaveTextContent(`${T}.requestFailed`);
  });

  it('clears an earlier error and result when submitting again', async () => {
    api.bulkTagFiles.mockRejectedValueOnce({});
    await open();
    await submitAdd('roadmap');
    await screen.findByRole('alert');

    await fireEvent.click(primary());
    await screen.findByRole('status');
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('removing', () => {
  it('never creates a tag and applies the pool name that normalizes the same', async () => {
    await open({ action: 'remove_tag' });
    await typeName('Q3_Review');
    await fireEvent.click(primary('remove'));

    await waitFor(() =>
      expect(api.bulkTagFiles).toHaveBeenCalledWith(files, 'remove_tag', 'q3 review')
    );
    expect(api.createTag).not.toHaveBeenCalled();
    const summary = await screen.findByRole('status');
    expect(summary).toHaveTextContent(`${T}.resolved {"name":"q3 review","typed":"Q3_Review"}`);
  });

  it('applies the typed name as is when nothing in the pool matches', async () => {
    await open({ action: 'remove_tag' });
    await typeName('unknown');
    await fireEvent.click(primary('remove'));
    await waitFor(() =>
      expect(api.bulkTagFiles).toHaveBeenCalledWith(files, 'remove_tag', 'unknown')
    );
  });

  it('resolves only against the scoped pool when there is one', async () => {
    await open({ action: 'remove_tag', presentTagNames: ['roadmap'] });
    await typeName('INTERVIEW');
    await fireEvent.click(primary('remove'));
    await waitFor(() =>
      expect(api.bulkTagFiles).toHaveBeenCalledWith(files, 'remove_tag', 'INTERVIEW')
    );
  });

  it('reports removed always and not-present only when non-zero', async () => {
    api.bulkTagFiles.mockResolvedValue(results(['removed', 'not_present', 'not_present']));
    const applied = vi.fn();
    await open({ action: 'remove_tag' }, { applied });
    await typeName('roadmap');
    await fireEvent.click(primary('remove'));
    const summary = await screen.findByRole('status');
    expect(summary).toHaveTextContent(`${T}.removedCount {"count":1,"name":"roadmap"}`);
    expect(summary).toHaveTextContent(`${T}.notPresentCount {"count":2}`);
    expect(summary).not.toHaveTextContent('alreadyPresentCount');
    expect(applied.mock.calls[0][0].detail).toEqual({
      action: 'remove_tag',
      name: 'roadmap',
      changed: 1,
      unchanged: 2,
      failed: 0,
    });
  });
});

describe('footer', () => {
  it('cancels before a result and says done after, both closing the dialog', async () => {
    const close = vi.fn();
    await open({}, { close });
    await fireEvent.click(screen.getByRole('button', { name: `${T}.cancel` }));
    expect(close).toHaveBeenCalledTimes(1);

    await submitAdd('roadmap');
    await screen.findByRole('status');
    expect(screen.queryByRole('button', { name: `${T}.cancel` })).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: `${T}.done` }));
    expect(close).toHaveBeenCalledTimes(2);
  });

  it('shows the applying label and locks both buttons while the batch runs', async () => {
    const pending = deferred<BulkTagActionResult[]>();
    api.bulkTagFiles.mockReturnValue(pending.promise);
    await open();
    await submitAdd('roadmap');

    const busyButton = await screen.findByRole('button', { name: `${T}.applying` });
    expect(busyButton).toBeDisabled();
    expect(screen.getByRole('button', { name: `${T}.cancel` })).toBeDisabled();

    pending.resolve(results(['added', 'added', 'added']));
    await screen.findByRole('status');
    expect(primary()).toBeEnabled();
  });
});

describe('current tags of the selection', () => {
  const carried = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      uuid: `c${String(i).padStart(2, '0')}`,
      name: `carried ${String(i).padStart(2, '0')}`,
      file_count: 1,
      selection_size: 1,
    }));

  it('shows no chips and no label when the selection carries nothing', async () => {
    await open();
    expect(screen.queryByText(`${T}.currentSingle`)).toBeNull();
    expect(screen.queryByText(`${T}.currentMulti`)).toBeNull();
  });

  it('gives a single file removable chips under the single label', async () => {
    api.listTagsOnFiles.mockResolvedValue(carried(2));
    await open({ fileUuids: ['solo'] });
    expect(await screen.findByText(`${T}.currentSingle`)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: `${T}.removeChip {"name":"carried 00"}` })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: `${T}.removeChip {"name":"carried 01"}` })
    ).toBeInTheDocument();
  });

  it('removes a chip through the files rail for that file, reloads and reports the outcome', async () => {
    api.listTagsOnFiles.mockResolvedValue(carried(1));
    api.bulkTagFiles.mockResolvedValue(results(['removed']));
    const applied = vi.fn();
    await open({ fileUuids: ['solo'] }, { applied });
    const chip = await screen.findByRole('button', {
      name: `${T}.removeChip {"name":"carried 00"}`,
    });
    api.listTagsOnFiles.mockClear();

    await fireEvent.click(chip);

    await waitFor(() =>
      expect(api.bulkTagFiles).toHaveBeenCalledWith(['solo'], 'remove_tag', 'carried 00')
    );
    await waitFor(() => expect(api.listTagsOnFiles).toHaveBeenCalledWith(['solo']));
    await waitFor(() => expect(applied).toHaveBeenCalledTimes(1));
    expect(applied.mock.calls[0][0].detail).toEqual({
      action: 'remove_tag',
      name: 'carried 00',
      changed: 1,
      unchanged: 0,
      failed: 0,
    });
    expect(api.createTag).not.toHaveBeenCalled();
  });

  it('keeps several files read-only with coverage counts under the multi label', async () => {
    api.listTagsOnFiles.mockResolvedValue([
      { uuid: 'c1', name: 'half', file_count: 1, selection_size: 3 },
      { uuid: 'c2', name: 'all', file_count: 3, selection_size: 3 },
    ]);
    await open();
    expect(await screen.findByText(`${T}.currentMulti`)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /removeChip/ })).toBeNull();
    const row = document.querySelector('.chip-row') as HTMLElement;
    expect(row).toHaveTextContent(`${T}.onCount {"count":1,"total":3}`);
    expect(row).toHaveTextContent(`${T}.onCount {"count":3,"total":3}`);
    // Full coverage sorts before partial coverage.
    expect(row.textContent!.indexOf('all')).toBeLessThan(row.textContent!.indexOf('half'));
  });

  it('caps the chips at 10 for several files and 40 for one, summarising the rest', async () => {
    api.listTagsOnFiles.mockResolvedValue(carried(45));
    const multi = await open();
    await screen.findByText(`${T}.currentMulti`);
    expect(document.querySelectorAll('.chip-slot')).toHaveLength(10);
    expect(screen.getByText(`${T}.moreTags {"count":35}`)).toBeInTheDocument();
    multi.unmount();

    await open({ fileUuids: ['solo'] });
    await screen.findByText(`${T}.currentSingle`);
    expect(document.querySelectorAll('.chip-slot')).toHaveLength(40);
    expect(screen.getByText(`${T}.moreTags {"count":5}`)).toBeInTheDocument();
  });

  it('treats a failed selection lookup as no chips', async () => {
    api.listTagsOnFiles.mockRejectedValue(new Error('x'));
    await open();
    await Promise.resolve();
    expect(screen.queryByText(`${T}.currentMulti`)).toBeNull();
    await typeName('roadmap');
    expect(primary()).toBeEnabled();
  });
});
