import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyWithOptionsLocale()
);

import TagList, { tagOriginKey } from './TagList.svelte';
import type { TagListEntry } from './TagList.svelte';
import type { TagCollisionCluster } from '$lib/types/tag';

const A = 'aaaaaaaa-0000-0000-0000-000000000001';
const B = 'bbbbbbbb-0000-0000-0000-000000000002';
const C = 'cccccccc-0000-0000-0000-000000000003';
const D = 'dddddddd-0000-0000-0000-000000000004';
const E = 'eeeeeeee-0000-0000-0000-000000000005';

const flat: TagListEntry[] = [
  { uuid: A, name: 'alpha', source: 'manual', usage_count: 3, can_write: true, ownership: 'mine' },
  {
    uuid: B,
    name: 'bravo',
    source: 'auto_ai',
    usage_count: 2,
    can_write: true,
    ownership: 'system',
  },
  {
    uuid: C,
    name: 'charlie',
    source: 'ai_accepted',
    usage_count: 1,
    can_write: false,
    ownership: 'shared_with_me',
  },
  { uuid: D, name: 'delta', source: null, usage_count: 0, can_write: true },
];

const clusters: TagCollisionCluster[] = [
  {
    normalized_name: 'one',
    members: [
      {
        uuid: A,
        name: 'One',
        source: 'manual',
        usage_count: 5,
        can_write: true,
        suggested_survivor: true,
      },
      {
        uuid: B,
        name: 'one',
        source: 'manual',
        usage_count: 1,
        can_write: true,
        suggested_survivor: false,
      },
    ],
    suggested_survivor_uuid: A,
    suggestions: [
      {
        uuid: E,
        name: 'on e',
        source: 'auto_ai',
        usage_count: 4,
        can_write: true,
        similarity: 0.846,
      },
    ],
  },
  {
    normalized_name: 'two',
    members: [
      {
        uuid: C,
        name: 'Two',
        source: 'manual',
        usage_count: 2,
        can_write: true,
        suggested_survivor: false,
      },
      {
        uuid: D,
        name: 'two',
        source: 'manual',
        usage_count: 1,
        can_write: true,
        suggested_survivor: true,
      },
    ],
    suggested_survivor_uuid: D,
    suggestions: [],
  },
];

function lastDetail(select: ReturnType<typeof vi.fn>) {
  return select.mock.calls[select.mock.calls.length - 1][0].detail;
}

describe('tagOriginKey', () => {
  it.each([
    ['manual', 'tags.manager.origin.manual'],
    ['auto_ai', 'tags.manager.origin.auto'],
    ['ai_accepted', 'tags.manager.origin.aiAccepted'],
    ['something_else', 'tags.manager.origin.unknown'],
    ['', 'tags.manager.origin.unknown'],
    [null, 'tags.manager.origin.unknown'],
    [undefined, 'tags.manager.origin.unknown'],
  ])('maps %j to %s', (source, key) => {
    expect(tagOriginKey(source)).toBe(key);
  });
});

describe('TagList flat rows', () => {
  it('is one multiselectable listbox named by the label prop', () => {
    render(TagList, { props: { tags: flat, label: 'my label' } });
    const box = screen.getByRole('listbox', { name: 'my label' });
    expect(screen.getAllByRole('listbox')).toHaveLength(1);
    expect(box).toHaveAttribute('aria-multiselectable', 'true');
    expect(within(box).getAllByRole('option')).toHaveLength(4);
  });

  it('renders name, usage text and origin label per row', () => {
    render(TagList, { props: { tags: flat, label: 'l' } });
    const rows = screen.getAllByRole('option');
    expect(rows[0]).toHaveTextContent('alpha');
    expect(rows[0]).toHaveTextContent('tags.manager.usage {"count":3}');
    expect(rows[0]).toHaveTextContent('tags.manager.origin.manual');
    expect(rows[1]).toHaveTextContent('tags.manager.origin.auto');
    expect(rows[2]).toHaveTextContent('tags.manager.origin.aiAccepted');
    expect(rows[3]).toHaveTextContent('tags.manager.usage {"count":0}');
    expect(rows[3]).toHaveTextContent('tags.manager.origin.unknown');
  });

  it('badges system and shared_with_me rows and leaves mine and unset rows bare', () => {
    render(TagList, { props: { tags: flat, label: 'l' } });
    const rows = screen.getAllByRole('option');

    const system = within(rows[1]).getByText('tags.manager.sharedBadge');
    expect(system).toHaveAttribute('title', 'tags.manager.sharedTooltip');
    expect(system).toHaveClass('badge-info');

    const shared = within(rows[2]).getByText('tags.manager.sharedWithMeBadge');
    expect(shared).toHaveAttribute('title', 'tags.manager.sharedWithMeTooltip');
    expect(shared).toHaveClass('badge-default');

    expect(rows[0].querySelector('.badge')).toBeNull();
    expect(rows[3].querySelector('.badge')).toBeNull();
    expect(rows[1].querySelector('.badge')?.textContent?.trim()).toBe('tags.manager.sharedBadge');
  });

  it('ignores the tags prop in cluster mode', () => {
    render(TagList, { props: { tags: flat, clusters: [clusters[1]], label: 'l' } });
    expect(screen.queryByText('alpha')).toBeNull();
    expect(screen.getAllByRole('listbox')).toHaveLength(1);
  });

  it('renders nothing selectable for an empty list and never emits', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: [], label: 'l' }, events: { select } });
    expect(screen.queryAllByRole('option')).toHaveLength(0);
    expect(screen.getByRole('listbox', { name: 'l' })).toBeInTheDocument();
  });
});

describe('TagList selection state', () => {
  it('marks each selected row aria-selected', () => {
    render(TagList, { props: { tags: flat, selectedUuids: [B, D], label: 'l' } });
    expect(screen.getAllByRole('option').map((r) => r.getAttribute('aria-selected'))).toEqual([
      'false',
      'true',
      'false',
      'true',
    ]);
  });

  it('dims pending rows and exposes them as aria-disabled', () => {
    render(TagList, { props: { tags: flat, pendingUuids: [C], label: 'l' } });
    const rows = screen.getAllByRole('option');
    expect(rows.map((r) => r.getAttribute('aria-disabled'))).toEqual([
      'false',
      'false',
      'true',
      'false',
    ]);
    expect(rows[2]).toHaveClass('is-pending');
    expect(rows[0]).not.toHaveClass('is-pending');
  });

  it('announces none when nothing is selected and the count otherwise', async () => {
    const { rerender } = render(TagList, { props: { tags: flat, label: 'l' } });
    expect(screen.getByRole('status')).toHaveTextContent('tags.manager.selectionNone');
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite');

    await rerender({ tags: flat, selectedUuids: [A], label: 'l' });
    expect(screen.getByRole('status')).toHaveTextContent('tags.manager.selectionCount {"count":1}');
  });
});

describe('TagList pointer', () => {
  it('replaces on a plain click', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: flat, label: 'l' }, events: { select } });
    await fireEvent.click(screen.getAllByRole('option')[2]);
    expect(lastDetail(select)).toEqual({ mode: 'replace', uuids: [C] });
  });

  it('toggles on ctrl+click and on meta+click', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: flat, label: 'l' }, events: { select } });
    await fireEvent.click(screen.getAllByRole('option')[0], { ctrlKey: true });
    expect(lastDetail(select)).toEqual({ mode: 'toggle', uuids: [A] });
    await fireEvent.click(screen.getAllByRole('option')[1], { metaKey: true });
    expect(lastDetail(select)).toEqual({ mode: 'toggle', uuids: [B] });
  });

  it('shift+click spans from the anchor to the clicked row, in either direction', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: flat, label: 'l' }, events: { select } });
    const rows = () => screen.getAllByRole('option');

    await fireEvent.click(rows()[1]);
    await fireEvent.click(rows()[3], { shiftKey: true });
    expect(lastDetail(select)).toEqual({ mode: 'range', uuids: [B, C, D] });

    await fireEvent.click(rows()[0], { shiftKey: true });
    expect(lastDetail(select)).toEqual({ mode: 'range', uuids: [A, B] });
  });

  it('a ctrl+click moves the anchor that the next shift+click uses', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: flat, label: 'l' }, events: { select } });
    const rows = () => screen.getAllByRole('option');

    await fireEvent.click(rows()[0]);
    await fireEvent.click(rows()[2], { ctrlKey: true });
    await fireEvent.click(rows()[3], { shiftKey: true });
    expect(lastDetail(select)).toEqual({ mode: 'range', uuids: [C, D] });
  });

  it('does nothing for a click or shift+click on a pending row', async () => {
    const select = vi.fn();
    render(TagList, {
      props: { tags: flat, pendingUuids: [B], label: 'l' },
      events: { select },
    });
    await fireEvent.click(screen.getAllByRole('option')[1]);
    await fireEvent.click(screen.getAllByRole('option')[1], { shiftKey: true });
    await fireEvent.click(screen.getAllByRole('option')[1], { ctrlKey: true });
    expect(select).not.toHaveBeenCalled();
  });
});

describe('TagList keyboard', () => {
  it('moves focus by one and clamps at both ends without selecting', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: flat, label: 'l' }, events: { select } });
    const tabbable = () =>
      screen.getAllByRole('option').findIndex((r) => r.getAttribute('tabindex') === '0');

    await fireEvent.keyDown(screen.getAllByRole('option')[0], { key: 'ArrowUp' });
    expect(tabbable()).toBe(0);

    for (const expected of [1, 2, 3, 3]) {
      await fireEvent.keyDown(screen.getAllByRole('option')[tabbable()], { key: 'ArrowDown' });
      expect(tabbable()).toBe(expected);
    }
    await fireEvent.keyDown(screen.getAllByRole('option')[3], { key: 'ArrowUp' });
    expect(tabbable()).toBe(2);
    expect(select).not.toHaveBeenCalled();
  });

  it('keeps exactly one row in the tab order', () => {
    render(TagList, { props: { tags: flat, label: 'l' } });
    const indexes = screen.getAllByRole('option').map((r) => r.getAttribute('tabindex'));
    expect(indexes).toEqual(['0', '-1', '-1', '-1']);
  });

  it('jumps to the last and first row with End and Home', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: flat, label: 'l' }, events: { select } });
    await fireEvent.keyDown(screen.getAllByRole('option')[0], { key: 'End' });
    expect(screen.getAllByRole('option')[3]).toHaveAttribute('tabindex', '0');
    await fireEvent.keyDown(screen.getAllByRole('option')[3], { key: 'Home' });
    expect(screen.getAllByRole('option')[0]).toHaveAttribute('tabindex', '0');
    expect(select).not.toHaveBeenCalled();
  });

  it('shift+End and shift+Home extend a range from the anchor', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: flat, label: 'l' }, events: { select } });
    await fireEvent.click(screen.getAllByRole('option')[1]);
    select.mockClear();

    await fireEvent.keyDown(screen.getAllByRole('option')[1], { key: 'End', shiftKey: true });
    expect(lastDetail(select)).toEqual({ mode: 'range', uuids: [B, C, D] });
    await fireEvent.keyDown(screen.getAllByRole('option')[3], { key: 'Home', shiftKey: true });
    expect(lastDetail(select)).toEqual({ mode: 'range', uuids: [A, B] });
  });

  it('Space toggles and Enter replaces', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags: flat, label: 'l' }, events: { select } });
    await fireEvent.keyDown(screen.getAllByRole('option')[2], { key: ' ' });
    expect(lastDetail(select)).toEqual({ mode: 'toggle', uuids: [C] });
    await fireEvent.keyDown(screen.getAllByRole('option')[1], { key: 'Enter' });
    expect(lastDetail(select)).toEqual({ mode: 'replace', uuids: [B] });
    expect(select).toHaveBeenCalledTimes(2);
  });

  it('prevents default scrolling for the keys it handles and ignores others', async () => {
    render(TagList, { props: { tags: flat, label: 'l' } });
    const row = () => screen.getAllByRole('option')[0];
    // fireEvent resolves to false when the handler called preventDefault.
    for (const key of ['ArrowDown', 'ArrowUp', 'Home', 'End', ' ', 'Enter']) {
      expect(await fireEvent.keyDown(row(), { key })).toBe(false);
    }
    expect(await fireEvent.keyDown(row(), { key: 'a' })).toBe(true);
  });

  it('ignores Space and Enter on a pending row', async () => {
    const select = vi.fn();
    render(TagList, {
      props: { tags: flat, pendingUuids: [A], label: 'l' },
      events: { select },
    });
    await fireEvent.keyDown(screen.getAllByRole('option')[0], { key: ' ' });
    await fireEvent.keyDown(screen.getAllByRole('option')[0], { key: 'Enter' });
    expect(select).not.toHaveBeenCalled();
  });

  it('clamps the roving focus when a refetch shortens the list', async () => {
    const { rerender } = render(TagList, { props: { tags: flat, label: 'l' } });
    await fireEvent.keyDown(screen.getAllByRole('option')[0], { key: 'End' });
    expect(screen.getAllByRole('option')[3]).toHaveAttribute('tabindex', '0');

    await rerender({ tags: flat.slice(0, 2), label: 'l' });
    expect(screen.getAllByRole('option').map((r) => r.getAttribute('tabindex'))).toEqual([
      '-1',
      '0',
    ]);
  });
});

describe('TagList cluster mode', () => {
  it('gives each cluster its own listbox named by its normalized name', () => {
    render(TagList, { props: { clusters } });
    const one = screen.getByRole('listbox', { name: 'one' });
    const two = screen.getByRole('listbox', { name: 'two' });
    expect(within(one).getAllByRole('option')).toHaveLength(3);
    expect(within(two).getAllByRole('option')).toHaveLength(3);
  });

  it('shows member count and select hint on the header and the badge on the suggested survivor only', () => {
    render(TagList, { props: { clusters } });
    const one = screen.getByRole('listbox', { name: 'one' });
    const [header, first, second] = within(one).getAllByRole('option');
    expect(header).toHaveTextContent('one');
    expect(header).toHaveTextContent('tags.manager.cluster.memberCount {"count":2}');
    expect(header).toHaveTextContent('tags.manager.cluster.select');
    expect(first).toHaveTextContent('tags.manager.cluster.suggested');
    expect(second).not.toHaveTextContent('tags.manager.cluster.suggested');
  });

  it('a header is selected only when every member is, and pending when any member is', () => {
    const header = () =>
      within(screen.getByRole('listbox', { name: 'one' })).getAllByRole('option')[0];

    const partial = render(TagList, { props: { clusters, selectedUuids: [A] } });
    expect(header()).toHaveAttribute('aria-selected', 'false');
    partial.unmount();

    const whole = render(TagList, { props: { clusters, selectedUuids: [A, B] } });
    expect(header()).toHaveAttribute('aria-selected', 'true');
    expect(header()).toHaveAttribute('aria-disabled', 'false');
    whole.unmount();

    render(TagList, { props: { clusters, selectedUuids: [A, B], pendingUuids: [B] } });
    expect(header()).toHaveAttribute('aria-disabled', 'true');
  });

  it('emits group with every member for click, ctrl+click, Space and Enter on a header', async () => {
    const select = vi.fn();
    render(TagList, { props: { clusters }, events: { select } });
    const header = () =>
      within(screen.getByRole('listbox', { name: 'two' })).getAllByRole('option')[0];

    await fireEvent.click(header());
    await fireEvent.click(header(), { ctrlKey: true });
    await fireEvent.keyDown(header(), { key: ' ' });
    await fireEvent.keyDown(header(), { key: 'Enter' });

    expect(select.mock.calls.map((call) => call[0].detail)).toEqual(
      Array(4).fill({ mode: 'group', uuids: [C, D] })
    );
  });

  it('emits a plain replace for a member row', async () => {
    const select = vi.fn();
    render(TagList, { props: { clusters }, events: { select } });
    await fireEvent.click(
      within(screen.getByRole('listbox', { name: 'one' })).getAllByRole('option')[2]
    );
    expect(lastDetail(select)).toEqual({ mode: 'replace', uuids: [B] });
  });

  it('expands a header into its members and de-duplicates when a range crosses clusters', async () => {
    const select = vi.fn();
    render(TagList, { props: { clusters }, events: { select } });
    const one = within(screen.getByRole('listbox', { name: 'one' })).getAllByRole('option');
    const two = within(screen.getByRole('listbox', { name: 'two' })).getAllByRole('option');

    await fireEvent.click(one[1]);
    await fireEvent.click(two[1], { shiftKey: true });
    expect(lastDetail(select)).toEqual({ mode: 'range', uuids: [A, B, C, D] });
  });

  it('moves keyboard focus across the header and members of every cluster as one sequence', async () => {
    render(TagList, { props: { clusters } });
    const all = () => screen.getAllByRole('option');
    expect(all()).toHaveLength(6);
    await fireEvent.keyDown(all()[0], { key: 'End' });
    expect(all().map((r) => r.getAttribute('tabindex'))).toEqual([
      '-1',
      '-1',
      '-1',
      '-1',
      '-1',
      '0',
    ]);
  });

  it('lists suggestions in a collapsed section that is not part of the listbox', async () => {
    const select = vi.fn();
    render(TagList, { props: { clusters }, events: { select } });

    const toggle = screen.getByRole('button', {
      name: /tags\.manager\.cluster\.alsoSimilar \{"count":1\}/,
    });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: /on e/ })).toBeNull();
    expect(screen.getAllByRole('listbox')).toHaveLength(2);
    // The second cluster has no suggestions, so only one section exists.
    expect(screen.getAllByRole('button', { name: /alsoSimilar/ })).toHaveLength(1);

    await fireEvent.click(toggle);
    const suggestion = screen.getByRole('button', { name: /on e/ });
    expect(within(screen.getByRole('listbox', { name: 'one' })).queryByText('on e')).toBeNull();
    expect(suggestion).toHaveTextContent('tags.manager.usage {"count":4}');
    expect(suggestion).toHaveTextContent('tags.manager.origin.auto');
    expect(suggestion).toHaveTextContent('tags.manager.cluster.similarity {"percent":85}');
    expect(suggestion).toHaveTextContent('tags.manager.cluster.addSuggestion {"name":"on e"}');

    await fireEvent.click(suggestion);
    expect(lastDetail(select)).toEqual({ mode: 'add', uuids: [E] });
  });
});
