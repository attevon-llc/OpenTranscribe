import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

/**
 * The list is presentational: it renders what the backend already computed and
 * dispatches selection *intent* upward. These tests therefore assert the DOM it
 * produces and the `select` payloads it emits — never a selection it owns,
 * because it owns none.
 */
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      let out = en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

import TagList from './TagList.svelte';
import type { TagCollisionCluster } from '$lib/types/tag';

const UUID_A = 'aaaaaaaa-0000-0000-0000-000000000001';
const UUID_B = 'bbbbbbbb-0000-0000-0000-000000000002';
const UUID_C = 'cccccccc-0000-0000-0000-000000000003';

const tags = [
  { uuid: UUID_A, name: 'interviews', source: 'manual', usage_count: 12, awaiting_review: false },
  { uuid: UUID_B, name: 'roadmap', source: 'auto_ai', usage_count: 4, awaiting_review: true },
  { uuid: UUID_C, name: 'archive', source: null, usage_count: 0, awaiting_review: false },
];

const cluster: TagCollisionCluster = {
  normalized_name: 'roadmap',
  members: [
    { uuid: UUID_A, name: 'Roadmap', source: 'manual', usage_count: 9, suggested_survivor: true },
    { uuid: UUID_B, name: 'roadmap', source: 'auto_ai', usage_count: 2, suggested_survivor: false },
  ],
  suggested_survivor_uuid: UUID_A,
  suggestions: [
    { uuid: UUID_C, name: 'road-map', source: 'manual', usage_count: 1, similarity: 0.91 },
  ],
};

describe('TagList — flat mode', () => {
  it('renders usage count and origin for every tag', () => {
    render(TagList, { props: { tags, label: 'Tags' } });

    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(3);

    expect(screen.getByText('interviews')).toBeInTheDocument();
    expect(screen.getByText('Used on 12 files')).toBeInTheDocument();
    expect(screen.getByText('Used on 4 files')).toBeInTheDocument();
    expect(screen.getByText('Used on 0 files')).toBeInTheDocument();

    expect(screen.getByText('Added by hand')).toBeInTheDocument();
    expect(screen.getByText('Added by AI')).toBeInTheDocument();
    expect(screen.getByText('Unknown origin')).toBeInTheDocument();
  });

  it('flags the tags the backend marked as awaiting review', () => {
    render(TagList, { props: { tags, label: 'Tags' } });
    expect(screen.getAllByText('Awaiting review')).toHaveLength(1);
  });

  it('marks the selected rows via aria-selected', () => {
    render(TagList, { props: { tags, selectedUuids: [UUID_B], label: 'Tags' } });
    const options = screen.getAllByRole('option');
    expect(options[0]).toHaveAttribute('aria-selected', 'false');
    expect(options[1]).toHaveAttribute('aria-selected', 'true');
  });

  it('replaces the selection on a plain click and toggles with ctrl', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags, label: 'Tags' }, events: { select } });
    const options = screen.getAllByRole('option');

    await fireEvent.click(options[0]);
    expect(select.mock.calls[0][0].detail).toEqual({ mode: 'replace', uuids: [UUID_A] });

    await fireEvent.click(options[1], { ctrlKey: true });
    expect(select.mock.calls[1][0].detail).toEqual({ mode: 'toggle', uuids: [UUID_B] });
  });

  it('extends the selection with Shift+Arrow from the keyboard anchor', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags, label: 'Tags' }, events: { select } });
    const options = screen.getAllByRole('option');

    // Space sets the anchor on the focused row without leaving the keyboard.
    await fireEvent.keyDown(options[0], { key: ' ' });
    expect(select.mock.calls[0][0].detail).toEqual({ mode: 'toggle', uuids: [UUID_A] });

    await fireEvent.keyDown(options[0], { key: 'ArrowDown', shiftKey: true });
    expect(select.mock.calls[1][0].detail).toEqual({ mode: 'range', uuids: [UUID_A, UUID_B] });

    // The anchor stays put, so extending again grows the same range.
    await fireEvent.keyDown(options[1], { key: 'ArrowDown', shiftKey: true });
    expect(select.mock.calls[2][0].detail).toEqual({
      mode: 'range',
      uuids: [UUID_A, UUID_B, UUID_C],
    });
  });

  it('moves the roving tabindex with a plain arrow key and selects nothing', async () => {
    const select = vi.fn();
    render(TagList, { props: { tags, label: 'Tags' }, events: { select } });
    let options = screen.getAllByRole('option');
    expect(options[0]).toHaveAttribute('tabindex', '0');
    expect(options[1]).toHaveAttribute('tabindex', '-1');

    await fireEvent.keyDown(options[0], { key: 'ArrowDown' });
    options = screen.getAllByRole('option');
    expect(options[0]).toHaveAttribute('tabindex', '-1');
    expect(options[1]).toHaveAttribute('tabindex', '0');
    expect(select).not.toHaveBeenCalled();
  });

  it('announces the selection count in a live region', () => {
    render(TagList, { props: { tags, selectedUuids: [UUID_A, UUID_B], label: 'Tags' } });
    expect(screen.getByRole('status')).toHaveTextContent('2 tags selected');
  });

  it('ignores rows with a mutation in flight', async () => {
    const select = vi.fn();
    render(TagList, {
      props: { tags, pendingUuids: [UUID_A], label: 'Tags' },
      events: { select },
    });
    const options = screen.getAllByRole('option');
    expect(options[0]).toHaveAttribute('aria-disabled', 'true');

    await fireEvent.click(options[0]);
    expect(select).not.toHaveBeenCalled();
  });
});

describe('TagList — collision clusters', () => {
  it('renders a cluster header above its indented members', () => {
    render(TagList, { props: { clusters: [cluster] } });

    expect(screen.getByRole('listbox', { name: 'roadmap' })).toBeInTheDocument();
    expect(screen.getByText('2 tags share this name')).toBeInTheDocument();
    expect(screen.getByText('Roadmap')).toBeInTheDocument();
    expect(screen.getByText('Used on 9 files')).toBeInTheDocument();
    expect(screen.getByText('Used on 2 files')).toBeInTheDocument();
    expect(screen.getByText('Suggested survivor')).toBeInTheDocument();
  });

  it('selects every member of the cluster from the header row', async () => {
    const select = vi.fn();
    render(TagList, { props: { clusters: [cluster] }, events: { select } });

    const [header] = screen.getAllByRole('option');
    await fireEvent.click(header);
    expect(select.mock.calls[0][0].detail).toEqual({
      mode: 'group',
      uuids: [UUID_A, UUID_B],
    });
  });

  it('adds a ranked near match to the selection from the "also similar" section', async () => {
    const select = vi.fn();
    render(TagList, { props: { clusters: [cluster] }, events: { select } });

    await fireEvent.click(screen.getByRole('button', { name: /Also similar \(1\)/ }));
    expect(screen.getByText('91% match')).toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: /road-map/ }));
    expect(select.mock.calls[0][0].detail).toEqual({ mode: 'add', uuids: [UUID_C] });
  });
});
