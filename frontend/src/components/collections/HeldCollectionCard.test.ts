/**
 * The computed "Legal hold" collection (legal-hold plan §5, Lane D): rendered only while
 * `held_count > 0`, chosen as a whole, with no share, edit or delete control.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/galleryToolbarMocks')).englishLocale()
);

import HeldCollectionCard from './HeldCollectionCard.svelte';

describe('HeldCollectionCard', () => {
  it('renders nothing while no held file is visible', () => {
    render(HeldCollectionCard, { props: { count: 0 } });
    expect(screen.queryByTestId('held-collection')).toBeNull();
  });

  it('shows the count and asks the gallery to filter to held files when chosen', async () => {
    const select = vi.fn();
    render(HeldCollectionCard, { props: { count: 4 }, events: { select } } as never);
    const card = screen.getByTestId('held-collection');
    expect(card).toHaveTextContent('Legal hold');
    expect(card).toHaveTextContent('Files: 4');
    await fireEvent.click(card);
    expect(select).toHaveBeenCalledTimes(1);
  });

  it('offers no share, edit or delete control', () => {
    render(HeldCollectionCard, { props: { count: 1 } });
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.getByTestId('held-collection')).toHaveTextContent(
      'cannot be edited, shared or deleted'
    );
  });
});
