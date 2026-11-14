import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('./searchTestFixtures')).enLocaleMock());
vi.mock('$lib/i18n', () => ({
  translateSpeakerLabel: (name: string) => name.replace(/^SPEAKER_(\d+)$/, 'Speaker $1'),
}));

import SearchResultInfoCard from './SearchResultInfoCard.svelte';
import { searchHit } from './searchTestFixtures';

describe('SearchResultInfoCard speakers', () => {
  it('shows unnamed speakers through the label helper and keeps named ones', () => {
    const { container } = render(SearchResultInfoCard, {
      props: { hit: searchHit({ speakers: ['SPEAKER_00', 'Alice'] }) },
    });
    const chips = [...(container.querySelector('.chips')?.querySelectorAll('.chip') ?? [])].map(
      (c) => c.textContent?.trim()
    );
    expect(chips).toEqual(['Speaker 00', 'Alice']);
  });
});
