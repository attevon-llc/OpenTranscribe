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

import TagFilterBar from './TagFilterBar.svelte';

describe('TagFilterBar', () => {
  it('shows the three views in order under a named tab strip', () => {
    render(TagFilterBar);
    const strip = screen.getByRole('tablist', { name: 'tags.manager.filterLabel' });
    const tabs = within(strip).getAllByRole('tab');
    expect(tabs.map((tab) => tab.textContent?.trim())).toEqual([
      'tags.manager.filter.all',
      'tags.manager.filter.unused',
      'tags.manager.filter.collisions',
    ]);
  });

  it.each([
    ['all', 0],
    ['unused', 1],
    ['colliding', 2],
  ] as const)('marks only the %s tab selected', (filter, index) => {
    render(TagFilterBar, { props: { filter } });
    const selected = screen.getAllByRole('tab').map((tab) => tab.getAttribute('aria-selected'));
    expect(selected).toEqual([0, 1, 2].map((i) => String(i === index)));
  });

  it('puts the count on the active tab only', () => {
    render(TagFilterBar, { props: { filter: 'unused', count: 7 } });
    const [all, unused, colliding] = screen.getAllByRole('tab');
    expect(all.querySelector('.tab-badge')).toBeNull();
    expect(unused.querySelector('.tab-badge')?.textContent).toBe('7');
    expect(colliding.querySelector('.tab-badge')).toBeNull();
  });

  it('shows a zero count but hides a null one', async () => {
    const { rerender } = render(TagFilterBar, { props: { filter: 'all', count: 0 } });
    expect(screen.getAllByRole('tab')[0].querySelector('.tab-badge')?.textContent).toBe('0');

    await rerender({ filter: 'all', count: null });
    expect(screen.getAllByRole('tab')[0].querySelector('.tab-badge')).toBeNull();
  });

  it('emits change with the id of the clicked tab', async () => {
    const change = vi.fn();
    render(TagFilterBar, { props: { filter: 'all' }, events: { change } });

    await fireEvent.click(screen.getAllByRole('tab')[2]);
    await fireEvent.click(screen.getAllByRole('tab')[1]);

    expect(change.mock.calls.map((call) => call[0].detail)).toEqual(['colliding', 'unused']);
  });

  it('offers the four ownership scopes, in order, valued with the API strings', () => {
    render(TagFilterBar);
    const select = screen.getByLabelText('tags.manager.scopeLabel') as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      'all',
      'mine',
      'system',
      'shared_with_me',
    ]);
    expect(Array.from(select.options).map((option) => option.textContent?.trim())).toEqual([
      'tags.manager.scope.all',
      'tags.manager.scope.mine',
      'tags.manager.scope.system',
      'tags.manager.scope.shared_with_me',
    ]);
  });

  it('reflects the scope prop in the select', () => {
    render(TagFilterBar, { props: { scope: 'system' } });
    expect((screen.getByLabelText('tags.manager.scopeLabel') as HTMLSelectElement).value).toBe(
      'system'
    );
  });

  it('emits scopeChange carrying each of the four values', async () => {
    const scopeChange = vi.fn();
    render(TagFilterBar, { events: { scopeChange } });
    const select = screen.getByLabelText('tags.manager.scopeLabel');

    for (const value of ['mine', 'system', 'shared_with_me', 'all']) {
      await fireEvent.change(select, { target: { value } });
    }

    expect(scopeChange.mock.calls.map((call) => call[0].detail)).toEqual([
      'mine',
      'system',
      'shared_with_me',
      'all',
    ]);
  });

  it('does not emit change when only the scope changes', async () => {
    const change = vi.fn();
    render(TagFilterBar, { events: { change } });
    await fireEvent.change(screen.getByLabelText('tags.manager.scopeLabel'), {
      target: { value: 'mine' },
    });
    expect(change).not.toHaveBeenCalled();
  });
});
