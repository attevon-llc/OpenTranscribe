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

import TagFilterBar from './TagFilterBar.svelte';

describe('TagFilterBar', () => {
  it('leaves the ownership picker usable in the views the server scopes', () => {
    for (const filter of ['all', 'unused'] as const) {
      const { unmount } = render(TagFilterBar, { props: { filter } });
      expect(screen.getByLabelText('Tag ownership')).toBeEnabled();
      unmount();
    }
  });

  it('disables the ownership picker in the collisions view, which takes no scope', () => {
    render(TagFilterBar, { props: { filter: 'colliding', scope: 'mine' } });
    expect(screen.getByLabelText('Tag ownership')).toBeDisabled();
  });

  it('emits each ownership value the server accepts', async () => {
    const scopeChange = vi.fn();
    render(TagFilterBar, { events: { scopeChange } });
    for (const value of ['mine', 'system', 'shared_with_me', 'all']) {
      await fireEvent.change(screen.getByLabelText('Tag ownership'), { target: { value } });
    }
    expect(scopeChange.mock.calls.map(([event]) => event.detail)).toEqual([
      'mine',
      'system',
      'shared_with_me',
      'all',
    ]);
  });
});
