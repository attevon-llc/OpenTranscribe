/** The plain-language "What does a legal hold do?" explainer (legal-hold plan, Lane D). */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/galleryToolbarMocks')).englishLocale()
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

describe('HoldExplainer layout', () => {
  // jsdom does no layout, so the rules that put the tip beside the toggle text are read from
  // the component's own CSS: the toggle must shrink to its text (not span the row and push the
  // tip to the far edge), and the tip must sit in the same flex row as the toggle.
  const css = (readFileSync(resolve(__dirname, 'HoldExplainer.svelte'), 'utf8').split(
    '<style>'
  )[1] ?? '') as string;
  const rule = (selector: string) =>
    css.match(new RegExp(`${selector.replace(/[.():]/g, '\\$&')}\\s*\\{([^}]*)\\}`))?.[1] ?? '';

  it('sizes the toggle to its text so the tip sits next to it', () => {
    expect(rule(':global(.expandable-header)')).toMatch(/inline-size:\s*auto/);
    expect(rule(':global(.expandable-title)')).toMatch(/flex:\s*0 1 auto/);
  });

  it('does not reserve a stretching column for the disclosure', () => {
    expect(css).not.toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)\s*auto/);
    expect(rule('.hold-explainer')).toMatch(/display:\s*flex/);
  });

  it('keeps the tip a sibling of the toggle button, never inside it', () => {
    const { container } = render(HoldExplainer);
    const tip = container.querySelector('.tip-slot');
    expect(tip).not.toBeNull();
    expect(tip?.closest('button')).toBeNull();
  });
});
