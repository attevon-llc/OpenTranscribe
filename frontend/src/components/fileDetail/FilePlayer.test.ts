import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

vi.mock('$components/VideoPlayer.svelte', async () => ({
  default: (await import('./VideoPlayerTestStub.svelte')).default,
}));
vi.mock('$components/WaveformPlayer.svelte', () => ({ default: () => {} }));

import FilePlayer from './FilePlayer.svelte';
import { seeks } from './VideoPlayerTestStub.svelte';
import type { MediaFileDetail } from '$lib/types/media';

const FILE: MediaFileDetail = {
  uuid: 'f1',
  filename: 'a.mp4',
  upload_time: '2026-01-01T00:00:00Z',
  content_type: 'video/mp4',
  status: 'completed',
  transcript_segments: [],
};

function setup(props: Record<string, unknown> = {}) {
  const schedule = vi.fn();
  const onSeekDone = vi.fn();
  const result = render(FilePlayer, {
    props: { file: FILE, videoUrl: 'https://s3/x', schedule, ...props },
    events: { initialSeekDone: onSeekDone },
  });
  return { ...result, schedule, onSeekDone, api: result.component };
}

beforeEach(() => {
  seeks.length = 0;
});

describe('FilePlayer', () => {
  it('performs the ?t= seek once the player element exists, and reports it consumed', async () => {
    const { onSeekDone } = setup({ initialSeekTime: 42 });

    await waitFor(() => expect(seeks).toEqual([42]));
    expect(onSeekDone).toHaveBeenCalledTimes(1);
  });

  it('does not seek without a deep link', async () => {
    const { schedule } = setup({ initialSeekTime: null });
    await waitFor(() => expect(schedule).toHaveBeenCalled());
    expect(seeks).toEqual([]);
  });

  it('waits for a stream URL before it can initialize', async () => {
    setup({ videoUrl: '', initialSeekTime: 5 });
    await Promise.resolve();
    expect(seeks).toEqual([]);
  });

  it('seeks with half a second of lead-in, never before zero', () => {
    const { api } = setup();
    api.seekToTime(10);
    api.seekToTime(0.2);
    expect(seeks).toEqual([9.5, 0]);
  });

  it('shows the waveform only for completed audio or video', () => {
    expect(setup().container.querySelectorAll('.waveform-section')).toHaveLength(1);
  });

  it.each<MediaFileDetail>([
    { ...FILE, status: 'processing' },
    { ...FILE, content_type: 'application/pdf' },
  ])('hides the waveform for %o', (file) => {
    expect(setup({ file }).container.querySelector('.waveform-section')).toBeNull();
  });
});
