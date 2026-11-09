import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyWithOptionsLocale()
);

import TagBulkSummary from './TagBulkSummary.svelte';
import type { TagListEntry } from './TagList.svelte';
import type { TagImpact } from '$lib/types/tag';

const P = '0a000000-0000-4000-8000-00000000000a';
const Q = '0b000000-0000-4000-8000-00000000000b';
const R = '0c000000-0000-4000-8000-00000000000c';

function tag(uuid: string, name: string, usage_count: number, extra: Partial<TagListEntry> = {}) {
  const base: TagListEntry = {
    uuid,
    name,
    source: 'manual',
    usage_count,
    ownership: 'mine',
    can_write: true,
  };
  return { ...base, ...extra };
}

const writable = [tag(P, 'notes', 3), tag(Q, 'Notes', 8), tag(R, 'note', 1)];
const preview: TagImpact = { tags: [], accessible_file_count: 6, total_file_count: 30 };

const button = (name: string) => screen.queryByRole('button', { name });
const confirmButton = () => document.querySelector('.modal-delete-button') as HTMLElement | null;
const cancelButton = () => document.querySelector('.modal-cancel-button') as HTMLElement | null;
const checked = () => (screen.getAllByRole('radio') as HTMLInputElement[]).find((r) => r.checked);

describe('the whole selection decides which actions exist', () => {
  it('offers no merge, delete or survivor picker when one tag is not writable', () => {
    const mixed = [writable[0], { ...writable[1], can_write: false }, writable[2]];
    render(TagBulkSummary, { props: { tags: mixed, canPromote: true } });
    expect(button('tags.manager.action.merge')).toBeNull();
    expect(button('tags.manager.action.delete')).toBeNull();
    expect(screen.queryAllByRole('radio')).toHaveLength(0);
    expect(screen.queryByRole('group', { name: 'tags.manager.bulk.survivorLegend' })).toBeNull();
  });

  it('still offers promote for mine tags the server will not let the caller write', () => {
    const granted = writable.map((entry) => ({ ...entry, can_write: false }));
    render(TagBulkSummary, { props: { tags: granted, canPromote: true } });
    expect(button('tags.manager.action.promote')).toBeInTheDocument();
    expect(button('tags.manager.action.merge')).toBeNull();
  });

  it('offers merge and delete but no promote for a writable mixed-ownership selection', () => {
    const mixed = [writable[0], { ...writable[1], ownership: 'shared_with_me' as const }];
    render(TagBulkSummary, { props: { tags: mixed, canPromote: true } });
    expect(button('tags.manager.action.merge')).toBeEnabled();
    expect(button('tags.manager.action.delete')).toBeEnabled();
    expect(button('tags.manager.action.promote')).toBeNull();
    expect(screen.getAllByRole('radio')).toHaveLength(2);
  });

  it('keeps the clear button whatever the gates say', async () => {
    const clear = vi.fn();
    const locked = writable.map((entry) => ({ ...entry, can_write: false, ownership: 'system' }));
    render(TagBulkSummary, {
      props: { tags: locked as TagListEntry[] },
      events: { clear },
    });
    expect(button('tags.manager.action.merge')).toBeNull();
    expect(button('tags.manager.action.promote')).toBeNull();
    expect(button('tags.manager.action.delete')).toBeNull();
    await fireEvent.click(button('tags.manager.action.clearSelection')!);
    expect(clear).toHaveBeenCalledTimes(1);
  });

  it('puts the actions in the order merge, promote, delete', () => {
    render(TagBulkSummary, { props: { tags: writable, canPromote: true } });
    const labels = Array.from(document.querySelectorAll('.actions button')).map(
      (b) => b.textContent?.trim()
    );
    expect(labels).toEqual([
      'tags.manager.action.merge',
      'tags.manager.action.promote',
      'tags.manager.action.delete',
    ]);
  });
});

describe('survivor preselection', () => {
  it('re-preselects the suggested survivor when the selected set changes to include it', async () => {
    const view = render(TagBulkSummary, { props: { tags: writable.slice(0, 2) } });
    expect(checked()?.value).toBe(Q);

    await view.rerender({ tags: writable, suggestedSurvivorUuid: R });
    expect(checked()?.value).toBe(R);
  });

  it('does not move the pick when the same tags arrive in another order', async () => {
    const view = render(TagBulkSummary, { props: { tags: writable } });
    await fireEvent.click(
      screen.getAllByRole('radio').find((r) => (r as HTMLInputElement).value === R)!
    );
    await view.rerender({ tags: [...writable].reverse() });
    expect(checked()?.value).toBe(R);
  });

  it('previews the merge with the preselected survivor when the user picks nothing', async () => {
    const previewMerge = vi.fn();
    render(TagBulkSummary, { props: { tags: writable }, events: { previewMerge } });
    await fireEvent.click(button('tags.manager.action.merge')!);
    expect(previewMerge.mock.calls.map((call) => call[0].detail)).toEqual([{ survivorUuid: Q }]);
  });
});

describe('the shared delete dialog', () => {
  it('names every selected tag in list order, not survivor order', () => {
    render(TagBulkSummary, { props: { tags: writable, deletePreview: preview } });
    expect(
      screen.getByText(
        'tags.manager.impact.deleteMessage {"name":"notes, Notes, note","accessible":6,"total":30}'
      )
    ).toBeInTheDocument();
  });

  it('opens again for a new preview after a confirm closed it', async () => {
    const confirmDelete = vi.fn();
    const view = render(TagBulkSummary, {
      props: { tags: writable, deletePreview: preview },
      events: { confirmDelete },
    });
    await fireEvent.click(confirmButton()!);
    expect(confirmDelete).toHaveBeenCalledTimes(1);
    expect(confirmButton()).toBeNull();

    await view.rerender({ tags: writable, deletePreview: { ...preview } });
    expect(confirmButton()).not.toBeNull();
  });

  it('opens again for a fresh preview object with no null in between', async () => {
    const view = render(TagBulkSummary, { props: { tags: writable, deletePreview: preview } });
    await fireEvent.click(cancelButton()!);
    expect(confirmButton()).toBeNull();
    await view.rerender({ tags: writable, deletePreview: { ...preview, total_file_count: 31 } });
    expect(confirmButton()).not.toBeNull();
    expect(screen.getByText(/"total":31/)).toBeInTheDocument();
  });

  it('refuses a confirm during any busy state and accepts it once idle', async () => {
    const confirmDelete = vi.fn();
    const view = render(TagBulkSummary, {
      props: { tags: writable, deletePreview: preview, busy: 'merge' },
      events: { confirmDelete },
    });
    await fireEvent.click(confirmButton()!);
    expect(confirmDelete).not.toHaveBeenCalled();

    await view.rerender({ tags: writable, deletePreview: { ...preview }, busy: null });
    await fireEvent.click(confirmButton()!);
    expect(confirmDelete).toHaveBeenCalledTimes(1);
  });
});

describe('merge preview confirm', () => {
  it('sends the survivor picked after the preview appeared', async () => {
    const confirmMerge = vi.fn();
    render(TagBulkSummary, {
      props: { tags: writable, mergePreview: preview },
      events: { confirmMerge },
    });
    await fireEvent.click(
      screen.getAllByRole('radio').find((r) => (r as HTMLInputElement).value === P)!
    );
    const confirm = document.querySelector('.merge-preview .btn-danger') as HTMLElement;
    await fireEvent.click(confirm);
    expect(confirmMerge.mock.calls.map((call) => call[0].detail)).toEqual([{ survivorUuid: P }]);
  });
});
