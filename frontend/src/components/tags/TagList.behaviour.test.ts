import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return {
    t: readable((key: string, opts?: Record<string, unknown>) =>
      opts ? `${key} ${JSON.stringify(opts)}` : key
    ),
  };
});

import TagList from './TagList.svelte';
import type { TagListEntry } from './TagList.svelte';
import type { TagCollisionCluster } from '$lib/types/tag';

const U1 = '10000000-0000-4000-8000-000000000001';
const U2 = '20000000-0000-4000-8000-000000000002';
const U3 = '30000000-0000-4000-8000-000000000003';
const U4 = '40000000-0000-4000-8000-000000000004';
const U5 = '50000000-0000-4000-8000-000000000005';
const U6 = '60000000-0000-4000-8000-000000000006';

function entry(uuid: string, name: string, extra: Partial<TagListEntry> = {}): TagListEntry {
  return { uuid, name, source: 'manual', usage_count: 1, can_write: true, ...extra };
}

const five = [
  entry(U1, 'budget'),
  entry(U2, 'calls'),
  entry(U3, 'design'),
  entry(U4, 'hiring'),
  entry(U5, 'launch'),
];

const grouped: TagCollisionCluster[] = [
  {
    normalized_name: 'sales call',
    members: [
      { ...entry(U1, 'Sales Call'), suggested_survivor: true, ownership: 'system' },
      { ...entry(U2, 'sales-call'), suggested_survivor: false, ownership: 'shared_with_me' },
      { ...entry(U3, 'sales_call'), suggested_survivor: false, ownership: 'mine' },
    ],
    suggested_survivor_uuid: U1,
    suggestions: [{ ...entry(U6, 'sales calls'), similarity: 0.95 }],
  },
  {
    normalized_name: 'q3',
    members: [
      { ...entry(U4, 'Q3'), suggested_survivor: true },
      { ...entry(U5, 'q3'), suggested_survivor: false },
    ],
    suggested_survivor_uuid: U4,
    suggestions: [],
  },
];

const options = () => screen.getAllByRole('option');
const details = (select: ReturnType<typeof vi.fn>) =>
  select.mock.calls.map((call) => call[0].detail);

describe('row state follows the props after mount', () => {
  it('moves aria-selected and the selected class as selectedUuids changes', async () => {
    const view = render(TagList, { props: { tags: five, label: 'tags' } });
    expect(options().filter((row) => row.classList.contains('is-selected'))).toHaveLength(0);

    await view.rerender({ tags: five, label: 'tags', selectedUuids: [U2, U4] });
    expect(options().map((row) => row.getAttribute('aria-selected'))).toEqual([
      'false',
      'true',
      'false',
      'true',
      'false',
    ]);
    expect(options()[1]).toHaveClass('is-selected');
    expect(options()[3]).toHaveClass('is-selected');

    await view.rerender({ tags: five, label: 'tags', selectedUuids: [U5] });
    expect(options().map((row) => row.classList.contains('is-selected'))).toEqual([
      false,
      false,
      false,
      false,
      true,
    ]);
    expect(options()[1]).toHaveAttribute('aria-selected', 'false');
  });

  it('moves aria-disabled and the pending class as pendingUuids changes', async () => {
    const view = render(TagList, { props: { tags: five, label: 'tags' } });
    await view.rerender({ tags: five, label: 'tags', pendingUuids: [U1, U3] });
    expect(options().map((row) => row.getAttribute('aria-disabled'))).toEqual([
      'true',
      'false',
      'true',
      'false',
      'false',
    ]);
    expect(options()[2]).toHaveClass('is-pending');

    await view.rerender({ tags: five, label: 'tags', pendingUuids: [] });
    expect(options().map((row) => row.classList.contains('is-pending'))).toEqual([
      false,
      false,
      false,
      false,
      false,
    ]);
    expect(options()[0]).toHaveAttribute('aria-disabled', 'false');
  });

  it('lets a row that stopped being pending be clicked again', async () => {
    const select = vi.fn();
    const view = render(TagList, {
      props: { tags: five, label: 'tags', pendingUuids: [U2] },
      events: { select },
    });
    await fireEvent.click(options()[1]);
    expect(select).not.toHaveBeenCalled();

    await view.rerender({ tags: five, label: 'tags', pendingUuids: [] });
    await fireEvent.click(options()[1]);
    expect(details(select)).toEqual([{ mode: 'replace', uuids: [U2] }]);
  });

  it('selects a cluster header once its last member becomes selected', async () => {
    const view = render(TagList, { props: { clusters: grouped, selectedUuids: [U1, U2] } });
    const header = () =>
      within(screen.getByRole('listbox', { name: 'sales call' })).getAllByRole('option')[0];
    expect(header()).toHaveAttribute('aria-selected', 'false');

    await view.rerender({ clusters: grouped, selectedUuids: [U1, U2, U3] });
    expect(header()).toHaveAttribute('aria-selected', 'true');
    expect(header()).toHaveClass('is-selected');

    await view.rerender({ clusters: grouped, selectedUuids: [U1, U2, U3], pendingUuids: [U3] });
    expect(header()).toHaveAttribute('aria-disabled', 'true');
    expect(header()).toHaveClass('is-pending');
  });
});

describe('keyboard ranges', () => {
  it('anchors at the row Shift+ArrowDown started from when nothing was activated', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: five, label: 'tags' }, events: { select } });
    await fireEvent.keyDown(options()[0], { key: 'ArrowDown' });
    await fireEvent.keyDown(options()[1], { key: 'ArrowDown', shiftKey: true });
    await fireEvent.keyDown(options()[2], { key: 'ArrowDown', shiftKey: true });
    expect(details(select)).toEqual([
      { mode: 'range', uuids: [U2, U3] },
      { mode: 'range', uuids: [U2, U3, U4] },
    ]);
  });

  it('shrinks the range back towards the anchor with Shift+ArrowUp', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: five, label: 'tags' }, events: { select } });
    await fireEvent.keyDown(options()[3], { key: ' ' });
    await fireEvent.keyDown(options()[3], { key: 'ArrowUp', shiftKey: true });
    await fireEvent.keyDown(options()[2], { key: 'ArrowUp', shiftKey: true });
    await fireEvent.keyDown(options()[1], { key: 'ArrowDown', shiftKey: true });
    expect(details(select)).toEqual([
      { mode: 'toggle', uuids: [U4] },
      { mode: 'range', uuids: [U3, U4] },
      { mode: 'range', uuids: [U2, U3, U4] },
      { mode: 'range', uuids: [U3, U4] },
    ]);
  });

  it('keeps the anchor through plain arrow moves, so a later shift+click spans from it', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: five, label: 'tags' }, events: { select } });
    await fireEvent.click(options()[1]);
    await fireEvent.keyDown(options()[1], { key: 'ArrowDown' });
    await fireEvent.keyDown(options()[2], { key: 'ArrowDown' });
    await fireEvent.click(options()[4], { shiftKey: true });
    expect(details(select)).toEqual([
      { mode: 'replace', uuids: [U2] },
      { mode: 'range', uuids: [U2, U3, U4, U5] },
    ]);
  });

  it('moves real focus to the target row', async () => {
    render(TagList, { props: { tags: five, label: 'tags' } });
    await fireEvent.keyDown(options()[0], { key: 'End' });
    await vi.waitFor(() => expect(document.activeElement).toBe(options()[4]));
    await fireEvent.keyDown(options()[4], { key: 'ArrowUp' });
    await vi.waitFor(() => expect(document.activeElement).toBe(options()[3]));
  });

  it('moves focus onto a pending row with Shift held but emits nothing for it', async () => {
    const select = vi.fn();
    render(TagList, {
      props: { tags: five, label: 'tags', pendingUuids: [U2] },
      events: { select },
    });
    await fireEvent.keyDown(options()[0], { key: 'ArrowDown', shiftKey: true });
    expect(select).not.toHaveBeenCalled();
    expect(options()[1]).toHaveAttribute('tabindex', '0');
  });

  it('expands a cluster header inside a keyboard range', async () => {
    const select = vi.fn();
    render(TagList, { props: { clusters: grouped }, events: { select } });
    // Rows: [header sales call, U1, U2, U3, header q3, U4, U5].
    await fireEvent.keyDown(options()[3], { key: 'Enter' });
    await fireEvent.keyDown(options()[3], { key: 'ArrowDown', shiftKey: true });
    expect(details(select)).toEqual([
      { mode: 'replace', uuids: [U3] },
      { mode: 'range', uuids: [U3, U4, U5] },
    ]);
  });
});

describe('cluster member badges', () => {
  const members = () =>
    within(screen.getByRole('listbox', { name: 'sales call' }))
      .getAllByRole('option')
      .slice(1);

  it('gives a system member the shared badge after the suggested badge', () => {
    render(TagList, { props: { clusters: grouped } });
    const badges = Array.from(members()[0].querySelectorAll('.badge'));
    expect(badges.map((badge) => badge.textContent?.trim())).toEqual([
      'tags.manager.cluster.suggested',
      'tags.manager.sharedBadge',
    ]);
    expect(badges[1]).toHaveAttribute('title', 'tags.manager.sharedTooltip');
  });

  it('gives a shared_with_me member the shared-with-me badge and a mine member none', () => {
    render(TagList, { props: { clusters: grouped } });
    const [, shared, mine] = members();
    expect(shared.querySelector('.badge')?.textContent?.trim()).toBe(
      'tags.manager.sharedWithMeBadge'
    );
    expect(shared.querySelector('.badge')).toHaveAttribute(
      'title',
      'tags.manager.sharedWithMeTooltip'
    );
    expect(mine.querySelectorAll('.badge')).toHaveLength(0);
  });
});

describe('suggestions of a cluster', () => {
  it('disables a pending suggestion and does not emit for it', async () => {
    const select = vi.fn();
    render(TagList, {
      props: { clusters: grouped, pendingUuids: [U6] },
      events: { select },
    });
    await fireEvent.click(screen.getByRole('button', { name: /alsoSimilar/ }));
    const suggestion = screen.getByRole('button', { name: /sales calls/ });
    expect(suggestion).toBeDisabled();
    await fireEvent.click(suggestion);
    expect(select).not.toHaveBeenCalled();
  });

  it('rounds the similarity to a whole percent', async () => {
    const nearly = [{ ...grouped[0], suggestions: [{ ...entry(U6, 'x'), similarity: 0.874 }] }];
    render(TagList, { props: { clusters: nearly } });
    await fireEvent.click(screen.getByRole('button', { name: /alsoSimilar/ }));
    expect(screen.getByRole('button', { name: /addSuggestion/ })).toHaveTextContent(
      'tags.manager.cluster.similarity {"percent":87}'
    );
  });
});
