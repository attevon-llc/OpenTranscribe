import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);
vi.mock('$lib/i18n', () => ({
  translateSpeakerLabel: (name: string) => name.replace(/^SPEAKER_(\d+)$/, 'Speaker $1'),
}));

import SpeakerStats from './SpeakerStats.svelte';

describe('SpeakerStats talk-time cards', () => {
  it('translates unnamed speakers and keeps assigned names', () => {
    const { container } = render(SpeakerStats, {
      props: {
        analytics: {
          talk_time: { by_speaker: { SPEAKER_00: 60, SPEAKER_01: 40 }, total: 100 },
        },
        speakerList: [
          { name: 'SPEAKER_00', display_name: '' },
          { name: 'SPEAKER_01', display_name: 'Alice' },
        ],
      },
    });
    const text = container.textContent ?? '';
    expect(text).toContain('Speaker 00');
    expect(text).toContain('Alice');
    expect(text).not.toContain('SPEAKER_00');
  });
});
