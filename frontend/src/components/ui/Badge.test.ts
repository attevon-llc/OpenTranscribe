import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import Badge from './Badge.svelte';

/** The rendered pill, independent of Svelte's scoping-hash class. */
function badge(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>('span.badge');
  if (!el) throw new Error(`no .badge rendered — got: ${container.innerHTML}`);
  return el;
}

describe('Badge', () => {
  it('applies the requested variant class and no other variant', () => {
    const { container } = render(Badge, { props: { variant: 'success' } });
    const el = badge(container);
    // `not.toBeNull()` on a `.badge.badge-success` selector was the whole test, so a
    // component emitting EVERY variant class at once passed it. Assert exclusivity.
    expect(el.classList.contains('badge-success')).toBe(true);
    expect([...el.classList].filter((c) => c.startsWith('badge-'))).toEqual(['badge-success']);
  });

  it('defaults to the default variant', () => {
    const { container } = render(Badge);
    expect([...badge(container).classList].filter((c) => c.startsWith('badge-'))).toEqual([
      'badge-default',
    ]);
  });

  it.each(['default', 'success', 'warning', 'error', 'info'] as const)(
    'renders the %s variant',
    (variant) => {
      const { container } = render(Badge, { props: { variant } });
      expect([...badge(container).classList].filter((c) => c.startsWith('badge-'))).toEqual([
        `badge-${variant}`,
      ]);
    }
  );

  it('omits the title attribute entirely when no title is given', () => {
    // Badge.svelte documents "Omitted by default so no empty `title` reaches the DOM" and
    // implements it as `title={title ?? undefined}` — an invariant nothing tested. An empty
    // `title=""` shows an empty tooltip box on hover and is announced as a blank label.
    const { container } = render(Badge, { props: { variant: 'info' } });
    expect(badge(container).hasAttribute('title')).toBe(false);
  });

  it('passes an explicit title through as the hover/AT description', () => {
    const { container } = render(Badge, {
      props: { variant: 'warning', title: 'Retrying after a transient failure' },
    });
    expect(badge(container).getAttribute('title')).toBe('Retrying after a transient failure');
  });
});
