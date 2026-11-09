/** The held-file banner says who holds the file, never why (legal-hold plan, Lane D). */
import { describe, it, expect, vi } from 'vitest';
import type { HoldAuthority } from '$lib/types/hold';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/galleryToolbarMocks')).englishLocale()
);

import HeldFileBanner from './HeldFileBanner.svelte';

describe('HeldFileBanner', () => {
  it('renders nothing for a file that is not held', () => {
    render(HeldFileBanner, { props: { hold: { held: false, held_by: [], hold_uuids: [] } } });
    expect(screen.queryByTestId('held-file-banner')).toBeNull();
  });

  it.each<[HoldAuthority[], string]>([
    [['organization'], 'An administrator of your organization placed it.'],
    [['platform'], 'The service operator placed it.'],
    [
      ['platform', 'organization'],
      'Your organization and the service operator have both placed one.',
    ],
  ])('says who placed it: %j', (heldBy, sentence) => {
    render(HeldFileBanner, { props: { hold: { held: true, held_by: heldBy, hold_uuids: [] } } });
    expect(
      screen.getByRole('heading', { name: 'This file is under a legal hold' })
    ).toBeInTheDocument();
    expect(screen.getByText(sentence)).toBeInTheDocument();
    expect(
      screen.getByText(/you can read, search, play, export and download it/)
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'What does a legal hold do?' })).toBeInTheDocument();
  });
});
