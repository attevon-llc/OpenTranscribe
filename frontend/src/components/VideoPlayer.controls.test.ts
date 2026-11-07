/**
 * The file-detail player's controls, with the REAL Video.js 10 elements (plan #1327, lane C).
 *
 * jsdom runs the custom elements and their store (it cannot decode media, so playback state is
 * driven through the element's own events), which lets these pin what the replacement adds over
 * the previous player: control labels in the UI language, a screen-reader status region, the 4x speed kept,
 * page-wide shortcuts that skip typing, and no video-only controls on audio. The browser-only
 * parts (layout, RTL mirroring, real playback) are in `backend/tests/e2e/test_media_playback.py`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import { tick } from 'svelte';

const { uiLocale } = vi.hoisted(() => ({ uiLocale: { code: 'en' } }));

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((key: string) => key), locale: readable(uiLocale.code) };
});
vi.mock('$lib/api/captions', () => ({
  fetchCaptionTrack: vi.fn(() => Promise.resolve('WEBVTT\n')),
}));

import VideoPlayer from './VideoPlayer.svelte';

const video = { uuid: 'f-1', status: 'completed', content_type: 'video/mp4' };
const audio = { uuid: 'f-2', status: 'completed', content_type: 'audio/mpeg' };

async function mount(file: Record<string, unknown>) {
  const result = render(VideoPlayer, { props: { videoUrl: 'blob:media', file } });
  await tick();
  const media = result.container.querySelector('#player') as HTMLMediaElement;
  // The store attaches to the element a microtask after the player connects.
  const host = media.closest('video-player, audio-player') as unknown as {
    store: { target: unknown };
  };
  await vi.waitFor(() => expect(host.store.target).not.toBeNull());
  return { ...result, media };
}

/** Make jsdom's element report playback, the way a browser's does, and tell the store. */
function startPlayback(media: HTMLMediaElement) {
  Object.defineProperty(media, 'paused', { configurable: true, value: false });
  media.dispatchEvent(new Event('play'));
  media.dispatchEvent(new Event('playing'));
}

const barPlayButton = (container: HTMLElement) =>
  container.querySelector('.controls-row media-play-button') as HTMLElement;

beforeEach(() => {
  uiLocale.code = 'en';
  document.documentElement.lang = 'en';
  document.documentElement.dir = 'ltr';
});

afterEach(() => {
  document.documentElement.lang = 'en';
  document.documentElement.dir = 'ltr';
});

describe('file-detail player controls (Video.js)', () => {
  it('names its controls in the UI language: the Arabic play button is not "Play"', async () => {
    uiLocale.code = 'ar';
    document.documentElement.lang = 'ar';
    document.documentElement.dir = 'rtl';
    const { container } = await mount(video);

    await vi.waitFor(() => {
      const label = barPlayButton(container).getAttribute('aria-label') ?? '';
      expect(label).not.toBe('');
      expect(label).not.toBe('Play');
      expect(label).toMatch(/[؀-ۿ]/);
    });
  });

  it('names its controls in English for an English UI', async () => {
    const { container } = await mount(video);
    await vi.waitFor(() =>
      expect(barPlayButton(container).getAttribute('aria-label')).toBe('Play')
    );
  });

  it('announces playback to screen readers through a status region', async () => {
    const { container, media } = await mount(video);
    const announcer = container.querySelector('media-status-announcer') as HTMLElement;
    expect(announcer.getAttribute('role')).toBe('status');

    startPlayback(media);
    await vi.waitFor(() => expect(announcer.textContent).toContain('Playing'));
  });

  it('offers the speeds including 4x in the settings menu', async () => {
    const { container } = await mount(video);
    await vi.waitFor(() => {
      const labels = Array.from(
        container.querySelectorAll('media-playback-rate-radio-group media-menu-radio-item')
      ).map((item) => item.textContent?.trim());
      expect(labels).toEqual(['0.5×', '0.75×', '1×', '1.25×', '1.5×', '1.75×', '2×', '4×']);
    });
  });

  it('takes shortcuts page-wide, but not while the viewer types', async () => {
    const { media } = await mount(video);
    const press = (target: Element, key: string) =>
      target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));

    press(document.body, 'm');
    await vi.waitFor(() => expect(media.muted).toBe(true));
    press(document.body, 'l');
    expect(media.loop).toBe(true);

    const box = document.createElement('textarea');
    document.body.append(box);
    press(box, 'm');
    press(box, 'l');
    await tick();
    expect(media.muted).toBe(true);
    expect(media.loop).toBe(true);
    box.remove();
  });

  it('shows captions, PiP and fullscreen for video and none of them for audio', async () => {
    const withVideo = await mount(video);
    for (const tag of ['media-captions-button', 'media-pip-button', 'media-fullscreen-button']) {
      expect(withVideo.container.querySelector(tag), tag).not.toBeNull();
    }
    withVideo.unmount();

    const withAudio = await mount(audio);
    expect(withAudio.media.tagName).toBe('AUDIO');
    expect(withAudio.container.querySelector('.controls-row media-play-button')).not.toBeNull();
    for (const tag of ['media-captions-button', 'media-pip-button', 'media-fullscreen-button']) {
      expect(withAudio.container.querySelector(tag), tag).toBeNull();
    }
  });
});
