/**
 * The player's caption track comes from the SERVER (`GET /files/{uuid}/captions`).
 *
 * It used to be built here, one WebVTT cue per diarized segment, so a minute-long speaker
 * turn covered the video with a paragraph, and the text was a client-side serialization
 * of transcript data (the #673 shape). These tests pin that the track is exactly the
 * server's WebVTT, that the page's redacted/original toggle reaches the request, and that
 * a refused request empties the track instead of falling back to the segments.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/svelte';
import { tick } from 'svelte';

const { fetchCaptionTrack, captionsOff } = vi.hoisted(() => ({
  fetchCaptionTrack: vi.fn(),
  captionsOff: vi.fn(() => false),
}));

vi.mock('$lib/player/plyrHandle', async () => {
  const { createFakePlayer } = await import('$lib/player/fakePlayer');
  return { createPlyrPlayer: createFakePlayer, viewerTurnedCaptionsOff: () => captionsOff() };
});
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, opts?: Record<string, string>) => string) => void) => {
      run((key: string, opts?: Record<string, string>) =>
        key === 'speaker.localizedLabel' ? `Speaker ${opts?.number}` : key
      );
      return () => {};
    },
  },
}));

vi.mock('$lib/api/captions', () => ({
  fetchCaptionTrack: (...args: unknown[]) => fetchCaptionTrack(...args),
}));

import VideoPlayer from './VideoPlayer.svelte';
import { fakePlayers } from '$lib/player/fakePlayer';

const SERVER_VTT =
  'WEBVTT\n\n1\n00:00:00.000 --> 00:00:03.000\n<v Alice><c.speaker>Alice</c>: So what we found\n';
const LONG_SEGMENT_TEXT = 'word '.repeat(200).trim();

const file = {
  uuid: 'file-uuid-1',
  status: 'completed',
  content_type: 'video/mp4',
  transcript_segments: [
    { start_time: 0, end_time: 90, text: LONG_SEGMENT_TEXT, speaker_label: 'SPEAKER_00' },
  ],
};

let blobs: Blob[] = [];

beforeEach(() => {
  fakePlayers.length = 0;
  blobs = [];
  fetchCaptionTrack.mockReset();
  captionsOff.mockReturnValue(false);
  vi.spyOn(URL, 'createObjectURL').mockImplementation((blob: Blob | MediaSource) => {
    blobs.push(blob as Blob);
    return `blob:captions-${blobs.length}`;
  });
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function mountPlayer(props: Record<string, unknown> = {}) {
  const result = render(VideoPlayer, { props: { videoUrl: 'blob:video', file, ...props } });
  await tick();
  expect(fakePlayers).toHaveLength(1);
  return result;
}

async function flush() {
  for (let i = 0; i < 5; i++) await Promise.resolve();
  await tick();
}

describe('caption track', () => {
  it('is the WebVTT the server cut, never one cue built from a whole segment', async () => {
    fetchCaptionTrack.mockResolvedValue(SERVER_VTT);
    const { component, container } = await mountPlayer();

    await (component as unknown as { updateSubtitles: () => Promise<void> }).updateSubtitles();

    expect(fetchCaptionTrack).toHaveBeenCalledWith({
      fileUuid: 'file-uuid-1',
      showOriginal: false,
      speakerPlaceholder: 'Speaker {number}',
    });
    expect(blobs).toHaveLength(1);
    const served = await blobs[0].text();
    expect(served).toBe(SERVER_VTT);
    expect(served).not.toContain(LONG_SEGMENT_TEXT);
    const track = container.querySelector('track[kind="captions"]') as HTMLTrackElement;
    expect(track.getAttribute('src') ?? track.src).toBe('blob:captions-1');
  });

  it('asks for the original when the page has revealed it, and refetches on toggle', async () => {
    fetchCaptionTrack.mockResolvedValue(SERVER_VTT);
    const { component, rerender } = await mountPlayer();
    await (component as unknown as { updateSubtitles: () => Promise<void> }).updateSubtitles();
    expect(fetchCaptionTrack).toHaveBeenLastCalledWith(
      expect.objectContaining({ showOriginal: false })
    );

    await rerender({ videoUrl: 'blob:video', file, showOriginal: true });
    await flush();

    expect(fetchCaptionTrack).toHaveBeenCalledTimes(2);
    expect(fetchCaptionTrack).toHaveBeenLastCalledWith(
      expect.objectContaining({ showOriginal: true })
    );
  });

  it('empties the track when the server refuses, with no client-side fallback', async () => {
    fetchCaptionTrack.mockRejectedValue(Object.assign(new Error('409'), { status: 409 }));
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { component } = await mountPlayer();

    await (component as unknown as { updateSubtitles: () => Promise<void> }).updateSubtitles();

    expect(blobs).toHaveLength(1);
    expect(await blobs[0].text()).toBe('WEBVTT\n');
  });

  it('does not request captions for audio playback', async () => {
    fetchCaptionTrack.mockResolvedValue(SERVER_VTT);
    const { component } = await mountPlayer({
      file: { ...file, content_type: 'audio/mpeg' },
    });

    await (component as unknown as { updateSubtitles: () => Promise<void> }).updateSubtitles();

    expect(fetchCaptionTrack).not.toHaveBeenCalled();
  });
});

/**
 * Captions come on by default once the track loads, unless the viewer switched them off in an
 * earlier session. Both go through the player seam (`captionsVisible`,
 * `setCaptionsVisible`), never the library directly (issue #1327).
 */
describe('captions on by default', () => {
  async function loadTrack() {
    fetchCaptionTrack.mockResolvedValue(SERVER_VTT);
    const { component, container } = await mountPlayer();
    const track = container.querySelector('track[kind="captions"]') as HTMLTrackElement;
    // jsdom has no TextTrack; the player only reads and sets its mode.
    const textTrack = { mode: 'disabled' };
    Object.defineProperty(track, 'track', { value: textTrack });

    await (component as unknown as { updateSubtitles: () => Promise<void> }).updateSubtitles();
    track.dispatchEvent(new Event('load'));
    return { player: fakePlayers[0], textTrack };
  }

  it('turns captions on through the player once the track has loaded', async () => {
    const { player, textTrack } = await loadTrack();

    expect(player.setCaptionsVisible).toHaveBeenCalledExactlyOnceWith(true);
    expect(player.captionsVisible).toBe(true);
    expect(textTrack.mode).toBe('hidden');
  });

  it('leaves them off when the viewer switched them off', async () => {
    captionsOff.mockReturnValue(true);
    const { player, textTrack } = await loadTrack();

    expect(player.setCaptionsVisible).not.toHaveBeenCalled();
    expect(player.captionsVisible).toBe(false);
    expect(textTrack.mode).toBe('hidden');
  });
});
