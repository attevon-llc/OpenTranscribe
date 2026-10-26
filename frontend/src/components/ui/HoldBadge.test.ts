/**
 * The "Legal hold" pill (issue #1276): absent unless the server says held, and it says WHO holds
 * the item (never why) in its hover text.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

import HoldBadge from './HoldBadge.svelte';

describe('HoldBadge', () => {
  it('renders nothing for a free item, an absent hold and a hold object that is not held', () => {
    for (const hold of [undefined, null, { held: false, held_by: [], hold_uuids: [] }]) {
      const { container, unmount } = render(HoldBadge, { props: { hold } });
      expect(container.querySelector('.badge')).toBeNull();
      unmount();
    }
  });

  it.each([
    [['organization'], /Held by your organization/],
    [['platform'], /Held by the service operator/],
    [['platform', 'organization'], /your organization and by the service operator/],
  ] as const)('names who holds it for %j', (held_by, title) => {
    render(HoldBadge, { props: { hold: { held: true, held_by: [...held_by], hold_uuids: [] } } });
    const badge = screen.getByText('Legal hold').closest('.badge') as HTMLElement;
    expect(badge.getAttribute('title')).toMatch(title);
  });
});
