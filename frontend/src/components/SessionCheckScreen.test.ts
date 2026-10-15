/**
 * The session-check shell paints before anyone is authenticated, so it must not look like a
 * page: an earlier version drew a card grid and an avatar that the real app then replaced with
 * something else ("a ghost of the page with the wrong components in the wrong place", #1243).
 * It may draw the real brand banner in the navbar's own geometry and a neutral loader, nothing
 * that implies a component, and none of the real app's selectors.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((k: string) => k), locale: readable('en') };
});

import SessionCheckScreen from './SessionCheckScreen.svelte';

describe('SessionCheckScreen shell', () => {
  it('draws only the brand banner and a loader, nothing that imitates page content', () => {
    const { container } = render(SessionCheckScreen, { props: { shell: true } });
    expect(screen.getByRole('status').getAttribute('aria-busy')).toBe('true');
    expect(container.querySelectorAll('img')).toHaveLength(1);
    expect(container.querySelector('.bar-fill')).not.toBeNull();
    // Anything shaped like a card, a toolbar or an account avatar is a ghost of a page.
    expect(container.querySelectorAll('.block, .card, .avatar, .check-grid')).toHaveLength(0);
  });

  it('carries none of the real app selectors an e2e readiness wait could latch onto', () => {
    const { container } = render(SessionCheckScreen, { props: { shell: true } });
    expect(
      container.querySelector('.navbar, nav, [class*="gallery-"], .search-page, .user-button')
    ).toBeNull();
  });

  it('shows only the brand mark on a public route', () => {
    const { container } = render(SessionCheckScreen, { props: { shell: false } });
    expect(container.querySelector('.check-bar')).toBeNull();
    expect(screen.getByAltText('OpenTranscribe')).toBeTruthy();
  });
});
