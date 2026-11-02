import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      // Mirror i18next's plural resolution: a `count` option selects the
      // `_one` / `_other` variant. Without it, pluralized keys fall through to
      // the raw key and every assertion on that copy fails for the wrong reason.
      const count = opts?.count;
      const plural =
        typeof count === 'number' ? (count === 1 ? `${key}_one` : `${key}_other`) : undefined;
      let out = (plural && en[plural]) ?? en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

import TagBulkSummary from './TagBulkSummary.svelte';
import type { TagImpact } from '$lib/types/tag';

const UUID_A = 'aaaaaaaa-0000-0000-0000-000000000001';
const UUID_B = 'bbbbbbbb-0000-0000-0000-000000000002';
const UUID_C = 'cccccccc-0000-0000-0000-000000000003';

const tags = [
  { uuid: UUID_A, name: 'roadmap', source: 'auto_ai', usage_count: 2, can_write: true },
  { uuid: UUID_B, name: 'Roadmap', source: 'manual', usage_count: 17, can_write: true },
  { uuid: UUID_C, name: 'road map', source: 'manual', usage_count: 5, can_write: true },
];

const impact: TagImpact = {
  tags: [],
  accessible_file_count: 4,
  total_file_count: 42,
};

describe('TagBulkSummary', () => {
  it('titles the pane with the selection size', () => {
    render(TagBulkSummary, { props: { tags } });
    expect(screen.getByRole('heading', { name: '3 tags selected' })).toBeInTheDocument();
  });

  it('preselects the highest-usage tag as survivor and lets the user switch', async () => {
    render(TagBulkSummary, { props: { tags } });

    const options = screen.getAllByRole('radio') as HTMLInputElement[];
    // Ordered by usage, so the preselected survivor is also the first row.
    expect(options.map((option) => option.value)).toEqual([UUID_B, UUID_C, UUID_A]);
    expect(options[0].checked).toBe(true);
    expect(screen.getByText('Used on 17 files')).toBeInTheDocument();
    expect(screen.getByText('Used on 5 files')).toBeInTheDocument();
    expect(screen.getByText('Used on 2 files')).toBeInTheDocument();

    await fireEvent.click(options[2]);
    expect((screen.getAllByRole('radio') as HTMLInputElement[])[2].checked).toBe(true);
    expect((screen.getAllByRole('radio') as HTMLInputElement[])[0].checked).toBe(false);
  });

  it("honours the backend's suggested survivor over raw usage", () => {
    render(TagBulkSummary, { props: { tags, suggestedSurvivorUuid: UUID_C } });
    const options = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(options.find((option) => option.checked)?.value).toBe(UUID_C);
  });

  it('routes merge through the impact preview and reports both counts', async () => {
    const previewMerge = vi.fn();
    const confirmMerge = vi.fn();
    const { rerender } = render(TagBulkSummary, {
      props: { tags },
      events: { previewMerge, confirmMerge },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Merge' }));
    expect(previewMerge.mock.calls[0][0].detail).toEqual({ survivorUuid: UUID_B });
    expect(confirmMerge).not.toHaveBeenCalled();

    await rerender({ tags, mergePreview: impact });
    expect(screen.getByText('Everything folds into "Roadmap".')).toBeInTheDocument();
    expect(screen.getByText('4 files you can see')).toBeInTheDocument();
    expect(screen.getByText('42 files in total')).toBeInTheDocument();

    const mergeButtons = screen.getAllByRole('button', { name: 'Merge' });
    await fireEvent.click(mergeButtons[mergeButtons.length - 1]);
    expect(confirmMerge.mock.calls[0][0].detail).toEqual({ survivorUuid: UUID_B });
  });

  it('shows a pending label and disables every control while merging', () => {
    render(TagBulkSummary, { props: { tags, mergePreview: impact, busy: 'merge' } });
    expect(screen.getByRole('button', { name: 'Merging…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Clear selection' })).toBeDisabled();
    for (const radio of screen.getAllByRole('radio')) expect(radio).toBeDisabled();
  });
});

describe('TagBulkSummary write gates', () => {
  // One non-writable uuid makes the whole merge, delete or promote request 404,
  // so the summary offers an action only when every selected tag allows it.
  const mine = tags.map((tag) => ({ ...tag, ownership: 'mine' as const, can_write: true }));

  it('offers no merge or delete when a selected tag is shared with me', () => {
    const selection = [
      mine[0],
      mine[1],
      { ...mine[2], ownership: 'shared_with_me' as const, can_write: false },
    ];
    render(TagBulkSummary, { props: { tags: selection } });

    expect(screen.queryByRole('button', { name: 'Merge' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
    expect(screen.queryAllByRole('radio')).toHaveLength(0);
  });

  it('offers merge and delete over a system tag only when the server allows its write', () => {
    const { unmount } = render(TagBulkSummary, {
      props: { tags: [mine[0], { ...mine[1], ownership: 'system' as const, can_write: false }] },
    });
    expect(screen.queryByRole('button', { name: 'Merge' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
    unmount();

    render(TagBulkSummary, {
      props: { tags: [mine[0], { ...mine[1], ownership: 'system' as const, can_write: true }] },
    });
    expect(screen.getByRole('button', { name: 'Merge' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('offers merge and delete over colleague tags an org admin may write', () => {
    const colleagues = mine.map((tag) => ({ ...tag, ownership: 'shared_with_me' as const }));
    render(TagBulkSummary, { props: { tags: colleagues } });
    expect(screen.getByRole('button', { name: 'Merge' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('offers no merge or delete when one owner-looking tag has can_write false', () => {
    render(TagBulkSummary, { props: { tags: [mine[0], { ...mine[1], can_write: false }] } });
    expect(screen.queryByRole('button', { name: 'Merge' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
  });

  it('offers promote only when every selected tag is mine', () => {
    const mixed = [mine[0], { ...mine[1], ownership: 'system' as const }];
    const { unmount } = render(TagBulkSummary, {
      props: { tags: mixed, canPromote: true },
    });
    expect(screen.queryByRole('button', { name: 'Share with everyone' })).toBeNull();
    unmount();

    render(TagBulkSummary, { props: { tags: mine, canPromote: true } });
    expect(screen.getByRole('button', { name: 'Share with everyone' })).toBeInTheDocument();
  });
});

describe('TagBulkSummary delete', () => {
  it('confirms a multi-tag delete in the same dialog a single delete uses', async () => {
    const previewDelete = vi.fn();
    const confirmDelete = vi.fn();
    const cancelDelete = vi.fn();
    const { rerender } = render(TagBulkSummary, {
      props: { tags },
      events: { previewDelete, confirmDelete, cancelDelete },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(previewDelete).toHaveBeenCalledTimes(1);

    await rerender({ tags, deletePreview: impact });
    // The shared ConfirmationModal, naming every tag and carrying both counts.
    expect(
      screen.getByText(
        /Delete "roadmap, Roadmap, road map"\? It is on 4 of your files, and 42 in total/
      )
    ).toBeInTheDocument();
    const confirm = document.querySelector('.modal-delete-button') as HTMLButtonElement;
    expect(confirm).toHaveTextContent('Delete');
    await fireEvent.click(confirm);
    expect(confirmDelete).toHaveBeenCalledTimes(1);
    expect(cancelDelete).not.toHaveBeenCalled();
  });

  it('reports a cancelled multi-tag delete', async () => {
    const cancelDelete = vi.fn();
    render(TagBulkSummary, { props: { tags, deletePreview: impact }, events: { cancelDelete } });

    await fireEvent.click(document.querySelector('.modal-cancel-button') as HTMLButtonElement);
    expect(cancelDelete).toHaveBeenCalledTimes(1);
  });
});
