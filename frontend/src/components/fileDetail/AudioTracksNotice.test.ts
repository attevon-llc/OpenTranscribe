/** The file page's audio-track panel and its language-mismatch notice (#1263). */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    (en[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_m, name) => String(vars?.[name] ?? ''));
  return { t: readable(t), locale: readable('en') };
});

import AudioTracksNotice from './AudioTracksNotice.svelte';

const STREAMS = [
  { index: 0, codec: 'aac', channels: 2, language: 'eng', default: true },
  { index: 1, codec: 'aac', channels: 2, language: 'por', title: 'Dub', default: false },
];

describe('AudioTracksNotice', () => {
  it('renders nothing for a single-track file', () => {
    const { container } = render(AudioTracksNotice, { streams: [STREAMS[0]] });
    expect(container.querySelector('[data-testid="audio-tracks"]')).toBeNull();
  });

  it('lists the tracks and marks the transcribed one', () => {
    const { container } = render(AudioTracksNotice, { streams: STREAMS, chosen: 1 });
    const rows = container.querySelectorAll('.track');
    expect(rows).toHaveLength(2);
    expect(rows[1].classList.contains('active')).toBe(true);
    expect(rows[1].textContent).toContain('Portuguese');
    expect(rows[1].textContent).toContain('Transcribed');
    expect(rows[0].textContent).not.toContain('Transcribed');
  });

  it('defaults to the first track when none was chosen', () => {
    const { container } = render(AudioTracksNotice, { streams: STREAMS, chosen: null });
    expect(container.querySelectorAll('.track')[0].classList.contains('active')).toBe(true);
  });

  it('shows no mismatch notice unless the server flagged one', () => {
    const { container } = render(AudioTracksNotice, { streams: STREAMS, trackLanguage: 'por' });
    expect(container.querySelector('[data-testid="audio-language-mismatch"]')).toBeNull();
  });

  it('names both languages in the notice and offers another track or language', async () => {
    const chosen = vi.fn();
    render(AudioTracksNotice, {
      props: {
        streams: STREAMS,
        chosen: 1,
        mismatch: true,
        trackLanguage: 'por',
        detectedLanguage: 'en',
      },
      events: { chooseTrack: chosen },
    });

    const note = screen.getByRole('note');
    expect(note.textContent).toContain('tagged Portuguese');
    expect(note.textContent).toContain('detected as English');
    await fireEvent.click(screen.getByRole('button', { name: 'Choose another track or language' }));
    expect(chosen).toHaveBeenCalledOnce();
  });

  it('does not offer re-processing when the viewer cannot', () => {
    render(AudioTracksNotice, {
      streams: STREAMS,
      mismatch: true,
      trackLanguage: 'por',
      detectedLanguage: 'en',
      canReprocess: false,
    });
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('offers the language alone for a single-track file whose tag disagrees', () => {
    const { container } = render(AudioTracksNotice, {
      streams: [STREAMS[1]],
      mismatch: true,
      trackLanguage: 'por',
      detectedLanguage: 'en',
    });
    expect(container.querySelector('.track')).toBeNull();
    expect(screen.getByRole('note')).toBeTruthy();
    // No other track to pick, but the language can still be forced.
    expect(screen.getByRole('button', { name: 'Choose a language' })).toBeTruthy();
  });
});
