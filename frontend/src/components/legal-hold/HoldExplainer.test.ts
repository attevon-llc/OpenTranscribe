/** The plain-language "What does a legal hold do?" explainer (legal-hold plan, Lane D). */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).englishLocale()
);

import HoldExplainer from './HoldExplainer.svelte';

describe('HoldExplainer', () => {
  it('starts collapsed and expands to what is kept, blocked, still works and who releases', async () => {
    render(HoldExplainer);
    const toggle = screen.getByRole('button', { name: 'What does a legal hold do?' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText(/^Still works:/)).toBeNull();

    await fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText(/^Kept exactly as it is:/)).toBeInTheDocument();
    expect(screen.getByText(/^Blocked until the hold is released:/)).toBeInTheDocument();
    expect(screen.getByText(/^Still works:/)).toBeInTheDocument();
    expect(
      screen.getByText(/^Only an administrator can place or release a hold/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/^A hold is not quarantine, the trash or deleting/)
    ).toBeInTheDocument();
  });

  it('offers the one-sentence summary behind an info button', async () => {
    render(HoldExplainer);
    await fireEvent.click(screen.getByRole('button', { name: 'More information' }));
    expect(await screen.findByText(/preserves this content exactly as it is/)).toBeInTheDocument();
  });

  it('never renders a hold reason, even if one is passed', () => {
    // A reason is for the administrators who manage the hold; the explainer is shown to
    // everyone who can see the file.
    render(HoldExplainer, { props: { expanded: true, reason: 'SECRET-MATTER-REASON' } as never });
    expect(screen.getByText(/^Still works:/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('SECRET-MATTER-REASON');
  });
});
