/**
 * The classification banner is the marking strip, nothing more. The login page used to pass it
 * `requireAcknowledgment`, which opened an English-only consent modal underneath LoginBanner's
 * translated one: two consent dialogs at once.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyWithOptionsLocale()
);

import ClassificationBanner from './ClassificationBanner.svelte';

describe('ClassificationBanner', () => {
  it('never opens a consent dialog of its own, whatever the caller passes', () => {
    render(ClassificationBanner, {
      props: { classification: 'CUI', requireAcknowledgment: true },
    } as never);

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('banner')).toHaveTextContent('CUI');
  });

  it('names the marking through a translated label, keeping the level verbatim', () => {
    render(ClassificationBanner, { props: { classification: 'SECRET', position: 'both' } });

    const expected = 'classificationBanner.markingLabel {"level":"SECRET"}';
    expect(screen.getByRole('banner')).toHaveAttribute('aria-label', expected);
    expect(screen.getByRole('contentinfo')).toHaveAttribute('aria-label', expected);
  });
});
