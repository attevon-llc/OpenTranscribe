import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('./searchTestFixtures')).enLocaleMock());
vi.mock('$lib/i18n', () => ({
  translateSpeakerLabel: (name: string) => name.replace(/^SPEAKER_(\d+)$/, 'Speaker $1'),
}));

import SearchOccurrence from './SearchOccurrence.svelte';

describe('SearchOccurrence speaker label', () => {
  it('shows an unnamed speaker through the transcript label helper, not the raw id', () => {
    const { container } = render(SearchOccurrence, {
      props: {
        fileUuid: 'f1',
        occurrence: { start_time: 3, speaker: 'SPEAKER_00', snippet: 'hi' } as never,
      },
    });
    expect(container.querySelector('.speaker')?.textContent?.trim()).toBe('Speaker 00');
  });

  it('leaves a named speaker unchanged', () => {
    const { container } = render(SearchOccurrence, {
      props: { fileUuid: 'f1', occurrence: { start_time: 3, speaker: 'Alice' } as never },
    });
    expect(container.querySelector('.speaker')?.textContent?.trim()).toBe('Alice');
  });
});
