/** The audio-track picker of the re-process wizard (#1263). */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    (en[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_m, name) => String(vars?.[name] ?? ''));
  return { t: readable(t), locale: readable('en') };
});

import AudioTrackSelect from './AudioTrackSelect.svelte';

const STREAMS = [
  { index: 0, codec: 'aac', channels: 2, language: 'eng', default: true },
  { index: 1, codec: 'aac', channels: 2, language: 'por', title: 'Dub', default: false },
];

describe('AudioTrackSelect', () => {
  it('renders nothing for a single-track file', () => {
    const { container } = render(AudioTrackSelect, { streams: [STREAMS[0]], value: 0 });
    expect(container.querySelector('select')).toBeNull();
  });

  it('labels each track from its container metadata and reflects the current choice', () => {
    render(AudioTrackSelect, { streams: STREAMS, value: 1 });
    const select = screen.getByLabelText('Audio track to transcribe') as HTMLSelectElement;
    const labels = Array.from(select.options).map((o) => o.textContent);
    expect(labels).toEqual([
      'Track 1: English · Stereo · AAC (default)',
      'Track 2: Portuguese · Stereo · AAC · Dub',
    ]);
    expect(select.value).toBe('1');
  });

  it('writes the picked track index back to the binding', async () => {
    render(AudioTrackSelect, { streams: STREAMS, value: 0 });
    const select = screen.getByLabelText('Audio track to transcribe') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: '1' } });
    expect(select.value).toBe('1');
  });
});
