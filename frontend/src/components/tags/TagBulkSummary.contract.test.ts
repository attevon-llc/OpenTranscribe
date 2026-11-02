import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return {
    t: readable((key: string, opts?: Record<string, unknown>) =>
      opts ? `${key} ${JSON.stringify(opts)}` : key
    ),
  };
});

import TagBulkSummary from './TagBulkSummary.svelte';
import type { TagListEntry } from './TagList.svelte';
import type { TagImpact } from '$lib/types/tag';

const A = 'aaaaaaaa-0000-0000-0000-000000000001';
const B = 'bbbbbbbb-0000-0000-0000-000000000002';
const C = 'cccccccc-0000-0000-0000-000000000003';
const D = 'dddddddd-0000-0000-0000-000000000004';

const mine = (
  uuid: string,
  name: string,
  usage_count: number,
  source = 'manual'
): TagListEntry => ({
  uuid,
  name,
  source,
  usage_count,
  ownership: 'mine',
});

const tags: TagListEntry[] = [
  mine(A, 'alpha', 2),
  mine(B, 'Bravo', 17),
  mine(C, 'charlie', 5, 'auto_ai'),
];

const impact: TagImpact = { tags: [], accessible_file_count: 4, total_file_count: 42 };

const radios = () => screen.getAllByRole('radio') as HTMLInputElement[];
const checkedUuid = () => radios().find((r) => r.checked)?.value;
const btn = (name: string) => screen.queryByRole('button', { name });

describe('TagBulkSummary survivor picker', () => {
  it('titles the pane with the count under a level-two heading', () => {
    render(TagBulkSummary, { props: { tags } });
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(
      'tags.manager.bulk.title {"count":3}'
    );
  });

  it('puts the radios in a fieldset with a legend and a hint', () => {
    render(TagBulkSummary, { props: { tags } });
    expect(
      screen.getByRole('group', { name: 'tags.manager.bulk.survivorLegend' })
    ).toBeInTheDocument();
    expect(screen.getByText('tags.manager.bulk.survivorHint')).toBeInTheDocument();
  });

  it('orders by usage descending and breaks ties by name', () => {
    const tied = [mine(A, 'zulu', 5), mine(B, 'alpha', 5), mine(C, 'mike', 9)];
    render(TagBulkSummary, { props: { tags: tied } });
    expect(radios().map((r) => r.value)).toEqual([C, B, A]);
  });

  it('shows name, usage and origin for each option', () => {
    render(TagBulkSummary, { props: { tags } });
    const labelOf = (uuid: string) =>
      radios()
        .find((r) => r.value === uuid)!
        .closest('label')!;
    expect(labelOf(C)).toHaveTextContent('charlie');
    expect(labelOf(C)).toHaveTextContent('tags.manager.usage {"count":5}');
    expect(labelOf(C)).toHaveTextContent('tags.manager.origin.auto');
    expect(labelOf(A)).toHaveTextContent('tags.manager.origin.manual');
  });

  it('preselects the highest-usage tag and highlights only the chosen option', () => {
    render(TagBulkSummary, { props: { tags } });
    expect(checkedUuid()).toBe(B);
    const chosen = radios().map((r) => r.closest('label')!.classList.contains('is-chosen'));
    expect(chosen).toEqual([true, false, false]);
  });

  it('prefers the suggested survivor when it is selected', () => {
    render(TagBulkSummary, { props: { tags, suggestedSurvivorUuid: A } });
    expect(checkedUuid()).toBe(A);
  });

  it('ignores a suggested survivor that is not part of the selection', () => {
    render(TagBulkSummary, { props: { tags, suggestedSurvivorUuid: D } });
    expect(checkedUuid()).toBe(B);
  });

  it('keeps the explicit pick when only the counts refresh, and re-preselects when the set changes', async () => {
    const view = render(TagBulkSummary, { props: { tags } });
    await fireEvent.click(radios().find((r) => r.value === A)!);
    expect(checkedUuid()).toBe(A);

    await view.rerender({
      tags: tags.map((tag) => ({ ...tag, usage_count: tag.usage_count + 100 })),
    });
    expect(checkedUuid()).toBe(A);

    await view.rerender({ tags: [tags[0], tags[1], mine(D, 'delta', 1)] });
    expect(checkedUuid()).toBe(B);
  });
});

describe('TagBulkSummary actions', () => {
  it('previews a merge with the picked survivor', async () => {
    const previewMerge = vi.fn();
    render(TagBulkSummary, { props: { tags }, events: { previewMerge } });
    await fireEvent.click(radios().find((r) => r.value === C)!);
    await fireEvent.click(btn('tags.manager.action.merge')!);
    expect(previewMerge.mock.calls.map((c) => c[0].detail)).toEqual([{ survivorUuid: C }]);
  });

  it('cannot merge a single tag', () => {
    render(TagBulkSummary, { props: { tags: [tags[0]] } });
    expect(btn('tags.manager.action.merge')).toBeDisabled();
  });

  it('emits previewDelete, promote and clear', async () => {
    const previewDelete = vi.fn();
    const promote = vi.fn();
    const clear = vi.fn();
    render(TagBulkSummary, {
      props: { tags, canPromote: true },
      events: { previewDelete, promote, clear },
    });
    await fireEvent.click(btn('tags.manager.action.delete')!);
    await fireEvent.click(btn('tags.manager.action.promote')!);
    await fireEvent.click(btn('tags.manager.action.clearSelection')!);
    expect(previewDelete).toHaveBeenCalledTimes(1);
    expect(promote).toHaveBeenCalledTimes(1);
    expect(clear).toHaveBeenCalledTimes(1);
  });

  it('hides promote without the capability or when no selected tag is mine', () => {
    const { unmount } = render(TagBulkSummary, { props: { tags, canPromote: false } });
    expect(btn('tags.manager.action.promote')).toBeNull();
    unmount();

    const shared: TagListEntry[] = [
      { ...tags[0], ownership: 'system' },
      { ...tags[1], ownership: 'shared_with_me' },
    ];
    render(TagBulkSummary, { props: { tags: shared, canPromote: true } });
    expect(btn('tags.manager.action.promote')).toBeNull();
  });

  it('treats a missing ownership as mine for the promote control', () => {
    const unset: TagListEntry[] = [
      { uuid: A, name: 'a', usage_count: 1 },
      { uuid: B, name: 'b', usage_count: 1 },
    ];
    render(TagBulkSummary, { props: { tags: unset, canPromote: true } });
    expect(btn('tags.manager.action.promote')).toBeInTheDocument();
  });

  it.each(['merge', 'delete', 'promote'] as const)('locks everything while %s runs', (busy) => {
    render(TagBulkSummary, { props: { tags, canPromote: true, busy } });
    expect(btn('tags.manager.action.merge')).toBeDisabled();
    expect(btn('tags.manager.action.delete')).toBeDisabled();
    expect(
      btn(busy === 'promote' ? 'tags.manager.action.promoting' : 'tags.manager.action.promote')
    ).toBeDisabled();
    expect(btn('tags.manager.action.clearSelection')).toBeDisabled();
    for (const radio of radios()) expect(radio).toBeDisabled();
  });

  it('leaves every control enabled when idle', () => {
    render(TagBulkSummary, { props: { tags, canPromote: true } });
    for (const name of [
      'tags.manager.action.merge',
      'tags.manager.action.delete',
      'tags.manager.action.promote',
      'tags.manager.action.clearSelection',
    ]) {
      expect(btn(name)).toBeEnabled();
    }
    for (const radio of radios()) expect(radio).toBeEnabled();
  });

  it('shows the preview loading row only while loading', async () => {
    const view = render(TagBulkSummary, { props: { tags } });
    expect(screen.queryByText('tags.manager.impact.loading')).toBeNull();
    await view.rerender({ tags, previewLoading: true });
    expect(screen.getByText('tags.manager.impact.loading')).toBeInTheDocument();
  });
});

describe('TagBulkSummary merge preview', () => {
  it('names the chosen survivor and both counts, and confirm or cancel emit', async () => {
    const confirmMerge = vi.fn();
    const cancelMerge = vi.fn();
    render(TagBulkSummary, {
      props: { tags, mergePreview: impact },
      events: { confirmMerge, cancelMerge },
    });

    expect(
      screen.getByRole('heading', { level: 3, name: 'tags.manager.impact.title' })
    ).toBeInTheDocument();
    expect(screen.getByText('tags.manager.bulk.mergeInto {"name":"Bravo"}')).toBeInTheDocument();
    expect(screen.getByText('tags.manager.impact.accessible {"count":4}')).toBeInTheDocument();
    expect(screen.getByText('tags.manager.impact.total {"count":42}')).toBeInTheDocument();
    expect(screen.getByText('tags.manager.impact.totalNote {"count":42}')).toBeInTheDocument();

    await fireEvent.click(radios().find((r) => r.value === A)!);
    expect(screen.getByText('tags.manager.bulk.mergeInto {"name":"alpha"}')).toBeInTheDocument();

    await fireEvent.click(screen.getAllByRole('button', { name: 'tags.manager.action.merge' })[1]);
    expect(confirmMerge.mock.calls.map((c) => c[0].detail)).toEqual([{ survivorUuid: A }]);

    await fireEvent.click(btn('tags.manager.action.cancel')!);
    expect(cancelMerge).toHaveBeenCalledTimes(1);
  });

  it('swaps the confirm label to merging and disables the panel while merging', () => {
    render(TagBulkSummary, { props: { tags, mergePreview: impact, busy: 'merge' } });
    expect(btn('tags.manager.action.merging')).toBeDisabled();
    expect(btn('tags.manager.action.cancel')).toBeDisabled();
  });

  it('shows no delete panel for a merge preview', () => {
    render(TagBulkSummary, { props: { tags, mergePreview: impact } });
    expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(1);
    expect(btn('tags.manager.action.deleting')).toBeNull();
  });
});

describe('TagBulkSummary delete preview', () => {
  it('shows the counts and emits confirmDelete and cancelDelete', async () => {
    const confirmDelete = vi.fn();
    const cancelDelete = vi.fn();
    render(TagBulkSummary, {
      props: { tags, deletePreview: impact },
      events: { confirmDelete, cancelDelete },
    });

    expect(
      screen.getByRole('heading', { level: 3, name: 'tags.manager.impact.title' })
    ).toBeInTheDocument();
    expect(screen.getByText('tags.manager.impact.accessible {"count":4}')).toBeInTheDocument();
    expect(screen.getByText('tags.manager.impact.total {"count":42}')).toBeInTheDocument();
    expect(screen.queryByText(/bulk\.mergeInto/)).toBeNull();

    await fireEvent.click(screen.getAllByRole('button', { name: 'tags.manager.action.delete' })[1]);
    expect(confirmDelete).toHaveBeenCalledTimes(1);

    await fireEvent.click(btn('tags.manager.action.cancel')!);
    expect(cancelDelete).toHaveBeenCalledTimes(1);
  });

  it('swaps the confirm label to deleting while deleting', () => {
    render(TagBulkSummary, { props: { tags, deletePreview: impact, busy: 'delete' } });
    expect(btn('tags.manager.action.deleting')).toBeDisabled();
  });
});
