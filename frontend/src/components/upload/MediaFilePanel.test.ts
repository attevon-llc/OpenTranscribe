/**
 * The upload panel used to say "Supported formats: MP3, WAV, OGG, FLAC, AAC, M4A, MP4,
 * WEBM". That was both too short (every format is accepted) and silent about what
 * happens in the player: an AIFF or an AVI transcribed fine and then sat at 00:00. The
 * panel now groups formats by what the player does with them, from
 * `$lib/utils/mediaFormats`.
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import MediaFilePanel from './MediaFilePanel.svelte';
import {
  AUDIO_ONLY_PREVIEW_FORMATS,
  BROWSER_PLAYABLE_FORMATS,
  CONVERTED_AUDIO_FORMATS,
} from '$lib/utils/mediaFormats';

function groups() {
  const { getByTestId } = render(MediaFilePanel, { props: { file: null } });
  return {
    playable: getByTestId('formats-playable').textContent ?? '',
    converted: getByTestId('formats-converted').textContent ?? '',
    audioOnly: getByTestId('formats-audio-only').textContent ?? '',
  };
}

describe('supported formats', () => {
  it('advertises AIFF as converted and AVI as audio-only, never as playable', () => {
    const { playable, converted, audioOnly } = groups();

    expect(converted).toContain('uploader.formatsConverted');
    expect(converted).toContain('AIFF');
    expect(audioOnly).toContain('uploader.formatsAudioOnly');
    expect(audioOnly).toContain('AVI');
    expect(playable).not.toMatch(/AIFF|AVI|WMA|WMV/);
  });

  it('lists every format of each group, in that group only', () => {
    const { playable, converted, audioOnly } = groups();

    for (const name of BROWSER_PLAYABLE_FORMATS) expect(playable).toContain(name);
    for (const name of CONVERTED_AUDIO_FORMATS) expect(converted).toContain(name);
    for (const name of AUDIO_ONLY_PREVIEW_FORMATS) expect(audioOnly).toContain(name);
    const all = [
      ...BROWSER_PLAYABLE_FORMATS,
      ...CONVERTED_AUDIO_FORMATS,
      ...AUDIO_ONLY_PREVIEW_FORMATS,
    ];
    expect(new Set(all).size).toBe(all.length);
  });
});
