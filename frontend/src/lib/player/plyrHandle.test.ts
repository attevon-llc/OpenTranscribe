/**
 * The Plyr adapter behind the player seam (issue #1327).
 *
 * The hosts used to build these Plyr configurations and call Plyr themselves. These tests pin
 * that the adapter builds exactly what each player built before (controls, settings, shortcut
 * scope, captions, fullscreen, self-hosted assets) and that every `PlayerHandle` method reaches
 * the Plyr call the hosts used to make.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const { FakePlyr, instances } = vi.hoisted(() => {
  const instances: unknown[] = [];
  class FakePlyr {
    media: HTMLMediaElement;
    config: Record<string, unknown>;
    captions = { toggled: false };
    speed = 1;
    fakeDuration = 0;
    seekedTo: number[] = [];
    handlers: Record<string, Array<() => void>> = {};
    play = vi.fn(() => Promise.resolve());
    pause = vi.fn();
    destroy = vi.fn();
    toggleCaptions = vi.fn((on: boolean) => {
      this.captions.toggled = on;
    });
    constructor(media: HTMLMediaElement, config: Record<string, unknown>) {
      this.media = media;
      this.config = config;
      instances.push(this);
    }
    get duration() {
      return this.fakeDuration;
    }
    get currentTime() {
      return this.media.currentTime;
    }
    set currentTime(value: number) {
      this.seekedTo.push(value);
    }
    on(event: string, cb: () => void) {
      (this.handlers[event] ||= []).push(cb);
    }
  }
  return { FakePlyr, instances };
});

vi.mock('plyr', () => ({ default: FakePlyr }));
vi.mock('plyr/dist/plyr.css', () => ({}));

import { createPlyrPlayer, plyrConfig, viewerTurnedCaptionsOff } from './plyrHandle';

type Fake = InstanceType<typeof FakePlyr>;

function build(
  options: Parameters<typeof createPlyrPlayer>[1] = { variant: 'full', audio: false }
) {
  const media = document.createElement('video');
  const handle = createPlyrPlayer(media, options);
  return { media, handle, plyr: instances[instances.length - 1] as Fake };
}

beforeEach(() => {
  instances.length = 0;
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('plyrConfig', () => {
  it('builds the file-detail video player with page-wide shortcuts and every video control', () => {
    vi.stubGlobal('navigator', { ...navigator, platform: 'Linux x86_64', userAgent: 'X11' });
    const config = plyrConfig({ variant: 'full', audio: false });
    expect(config.controls).toEqual([
      'play-large',
      'play',
      'current-time',
      'duration',
      'progress',
      'mute',
      'volume',
      'captions',
      'settings',
      'pip',
      'fullscreen',
    ]);
    expect(config.settings).toEqual(['captions', 'speed']);
    expect(config.keyboard).toEqual({ global: true });
    expect(config.tooltips).toEqual({ controls: true });
    expect(config.captions).toEqual({ active: true, language: 'auto', update: true });
    expect(config.fullscreen).toEqual({ iosNative: true });
    expect(config.iconUrl).toBe('/plyr.svg');
    expect(config.blankVideo).toBe('/plyr-blank.mp4');
  });

  it('adds AirPlay on Apple platforms only', () => {
    vi.stubGlobal('navigator', { ...navigator, platform: 'MacIntel' });
    expect(plyrConfig({ variant: 'full', audio: false }).controls).toContain('airplay');
    vi.stubGlobal('navigator', { ...navigator, platform: 'Win32', userAgent: 'Windows' });
    expect(plyrConfig({ variant: 'full', audio: false }).controls).not.toContain('airplay');
  });

  it('gives the file-detail audio player no captions, PiP or fullscreen, and speed only', () => {
    vi.stubGlobal('navigator', { ...navigator, platform: 'Linux x86_64', userAgent: 'X11' });
    const config = plyrConfig({ variant: 'full', audio: true });
    expect(config.controls).toEqual([
      'play-large',
      'play',
      'current-time',
      'duration',
      'progress',
      'mute',
      'volume',
      'settings',
    ]);
    expect(config.settings).toEqual(['speed']);
  });

  it('scopes the mini player shortcuts to its focus and keeps the full-window fallback', () => {
    const video = plyrConfig({ variant: 'mini', audio: false });
    expect(video.controls).toEqual([
      'play',
      'current-time',
      'duration',
      'progress',
      'mute',
      'settings',
      'fullscreen',
    ]);
    expect(video.settings).toEqual(['captions', 'speed']);
    expect(video.keyboard).toEqual({ global: false });
    expect(video.fullscreen).toEqual({ iosNative: false, fallback: true });

    const audio = plyrConfig({ variant: 'mini', audio: true });
    expect(audio.controls).toEqual([
      'play',
      'current-time',
      'duration',
      'progress',
      'mute',
      'volume',
      'settings',
    ]);
    expect(audio.settings).toEqual(['speed']);
    expect(audio.iconUrl).toBe('/plyr.svg');
    expect(audio.blankVideo).toBe('/plyr-blank.mp4');
  });
});

describe('createPlyrPlayer', () => {
  it('constructs Plyr on the given element with the host configuration', () => {
    const { media, handle, plyr } = build({ variant: 'mini', audio: true });
    expect(plyr.media).toBe(media);
    expect(handle.media).toBe(media);
    expect(plyr.config).toEqual(plyrConfig({ variant: 'mini', audio: true }));
  });

  it('calls onReady when Plyr reports ready', () => {
    const onReady = vi.fn();
    const { plyr } = build({ variant: 'full', audio: false, onReady });
    expect(onReady).not.toHaveBeenCalled();
    plyr.handlers.ready.forEach((cb) => cb());
    expect(onReady).toHaveBeenCalledTimes(1);
  });

  it('play and pause reach Plyr, and play always returns a promise', async () => {
    const { handle, plyr } = build();
    await expect(handle.play()).resolves.toBeUndefined();
    expect(plyr.play).toHaveBeenCalledTimes(1);

    plyr.play.mockReturnValueOnce(undefined as unknown as Promise<void>);
    await expect(handle.play()).resolves.toBeUndefined();

    const refused = new DOMException('blocked', 'NotAllowedError');
    plyr.play.mockReturnValueOnce(Promise.reject(refused));
    await expect(handle.play()).rejects.toBe(refused);

    handle.pause();
    expect(plyr.pause).toHaveBeenCalledTimes(1);
  });

  it('seeks through Plyr once it knows the duration', () => {
    const { handle, plyr, media } = build();
    plyr.fakeDuration = 120;
    handle.seek(42);
    expect(plyr.seekedTo).toEqual([42]);
    expect(media.currentTime).toBe(0);
  });

  it('seeks the element directly before metadata, where Plyr would drop the seek', () => {
    const { handle, plyr, media } = build();
    handle.seek(42);
    expect(plyr.seekedTo).toEqual([]);
    expect(media.currentTime).toBe(42);
  });

  it('sets the playback rate through Plyr', () => {
    const { handle, plyr } = build();
    handle.setPlaybackRate(1.5);
    expect(plyr.speed).toBe(1.5);
  });

  it('reports and switches the caption state Plyr keeps', () => {
    const { handle, plyr } = build();
    expect(handle.captionsVisible).toBe(false);
    handle.setCaptionsVisible(true);
    expect(plyr.toggleCaptions).toHaveBeenCalledWith(true);
    expect(handle.captionsVisible).toBe(true);
    handle.setCaptionsVisible(false);
    expect(handle.captionsVisible).toBe(false);
  });

  it('destroys Plyr', () => {
    const { handle, plyr } = build();
    handle.destroy();
    expect(plyr.destroy).toHaveBeenCalledTimes(1);
  });
});

describe('viewerTurnedCaptionsOff', () => {
  it('is false when nothing is stored', () => {
    expect(viewerTurnedCaptionsOff()).toBe(false);
  });

  it("reads Plyr's stored captions choice", () => {
    localStorage.setItem('plyr', JSON.stringify({ captions: false, volume: 1 }));
    expect(viewerTurnedCaptionsOff()).toBe(true);
    localStorage.setItem('plyr', JSON.stringify({ captions: true }));
    expect(viewerTurnedCaptionsOff()).toBe(false);
  });

  it('treats an unreadable value as not switched off', () => {
    localStorage.setItem('plyr', '{not json');
    expect(viewerTurnedCaptionsOff()).toBe(false);
  });
});
