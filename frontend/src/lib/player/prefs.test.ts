/**
 * Remembered player preferences (plan #1327 row 35, and row 23: captions on unless the
 * viewer switched them off). Video.js leaves persistence to the app
 * (https://videojs.org/docs/framework/html/guides/user-preferences), so this is ours.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_PLAYER_PREFS,
  LEGACY_PLYR_KEY,
  PLAYER_PREFS_KEY,
  bindPlayerPrefs,
  loadPlayerPrefs,
  savePlayerPrefs,
} from './prefs';
import { asMedia, fakeMedia, fakeStore } from './fakeVideojs';

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

const stored = () => JSON.parse(localStorage.getItem(PLAYER_PREFS_KEY) ?? 'null');

describe('loadPlayerPrefs / savePlayerPrefs', () => {
  it('defaults to full volume, unmuted, 1x and captions ON', () => {
    expect(loadPlayerPrefs()).toEqual({ volume: 1, muted: false, rate: 1, captions: true });
    expect(DEFAULT_PLAYER_PREFS.captions).toBe(true);
  });

  it('round-trips every field', () => {
    const prefs = { volume: 0.35, muted: true, rate: 1.75, captions: false };
    savePlayerPrefs(prefs);
    expect(stored()).toEqual(prefs);
    expect(loadPlayerPrefs()).toEqual(prefs);
  });

  it('replaces each invalid field with its default and keeps the valid ones', () => {
    localStorage.setItem(
      PLAYER_PREFS_KEY,
      JSON.stringify({ volume: 7, muted: 'yes', rate: -1, captions: false })
    );
    expect(loadPlayerPrefs()).toEqual({ volume: 1, muted: false, rate: 1, captions: false });
  });

  it('falls back to the defaults on corrupt JSON', () => {
    localStorage.setItem(PLAYER_PREFS_KEY, '{not json');
    expect(loadPlayerPrefs()).toEqual(DEFAULT_PLAYER_PREFS);
  });

  it('tolerates a localStorage whose reads and writes throw', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });
    expect(loadPlayerPrefs()).toEqual(DEFAULT_PLAYER_PREFS);
    expect(() => savePlayerPrefs({ ...DEFAULT_PLAYER_PREFS, volume: 0.2 })).not.toThrow();
  });

  it('works with no storage at all', () => {
    expect(loadPlayerPrefs(null)).toEqual(DEFAULT_PLAYER_PREFS);
    expect(() => savePlayerPrefs(DEFAULT_PLAYER_PREFS, null)).not.toThrow();
  });
});

describe('legacy Plyr preferences', () => {
  it('carries a viewer’s Plyr choices over once, including captions switched off', () => {
    localStorage.setItem(
      LEGACY_PLYR_KEY,
      JSON.stringify({ volume: 0.4, muted: true, speed: 1.5, captions: false, language: 'en' })
    );
    expect(loadPlayerPrefs()).toEqual({ volume: 0.4, muted: true, rate: 1.5, captions: false });
    // Migrated under the new key, so the legacy blob is read only this once.
    expect(stored()).toEqual({ volume: 0.4, muted: true, rate: 1.5, captions: false });
  });

  it('ignores the legacy key once the new key exists', () => {
    savePlayerPrefs({ volume: 0.9, muted: false, rate: 1, captions: true });
    localStorage.setItem(LEGACY_PLYR_KEY, JSON.stringify({ captions: false, speed: 2 }));
    expect(loadPlayerPrefs()).toEqual({ volume: 0.9, muted: false, rate: 1, captions: true });
  });

  it('fills fields Plyr never wrote with defaults', () => {
    localStorage.setItem(LEGACY_PLYR_KEY, JSON.stringify({ captions: false }));
    expect(loadPlayerPrefs()).toEqual({ ...DEFAULT_PLAYER_PREFS, captions: false });
  });
});

describe('bindPlayerPrefs', () => {
  it('restores volume, mute and rate through the store once it attaches', () => {
    savePlayerPrefs({ volume: 0.3, muted: true, rate: 1.5, captions: true });
    const media = fakeMedia();
    const f = fakeStore(media, { attached: false });
    bindPlayerPrefs(f.store, asMedia(media));
    expect(f.store.setVolume).not.toHaveBeenCalled();

    f.attach();
    expect(f.store.setVolume).toHaveBeenCalledWith(0.3);
    expect(f.store.setMuted).toHaveBeenCalledWith(true);
    expect(f.store.setPlaybackRate).toHaveBeenCalledWith(1.5);
    expect([media.volume, media.muted, media.playbackRate]).toEqual([0.3, true, 1.5]);
  });

  it('does not overwrite the saved rate while the store still reports the old one', () => {
    savePlayerPrefs({ volume: 1, muted: false, rate: 2, captions: true });
    const media = fakeMedia();
    const f = fakeStore(media);
    bindPlayerPrefs(f.store, asMedia(media));
    f.notify();
    expect(stored().rate).toBe(2);
  });

  it('saves later changes the viewer makes', () => {
    const media = fakeMedia();
    const f = fakeStore(media);
    bindPlayerPrefs(f.store, asMedia(media));
    media.volume = 0.6;
    media.playbackRate = 0.75;
    f.notify();
    expect(stored()).toEqual({ volume: 0.6, muted: false, rate: 0.75, captions: true });
  });

  it('turns captions on when the first caption track arrives, by default', () => {
    const media = fakeMedia();
    const f = fakeStore(media);
    bindPlayerPrefs(f.store, asMedia(media));
    media.textTracks.push({ kind: 'captions', mode: 'disabled' });
    f.notify();
    expect(f.store.toggleSubtitles).toHaveBeenCalledWith(true);
    expect(media.textTracks[0].mode).toBe('showing');
  });

  it('keeps captions off for a viewer who switched them off, across a track reload', () => {
    const media = fakeMedia();
    const f = fakeStore(media);
    bindPlayerPrefs(f.store, asMedia(media));
    media.textTracks.push({ kind: 'captions', mode: 'disabled' });
    f.notify(); // restored: on
    media.textTracks[0].mode = 'disabled'; // viewer presses CC
    f.notify();
    expect(stored().captions).toBe(false);

    media.textTracks.splice(0, 1); // the app swaps the blob-URL track after an edit
    f.notify();
    expect(stored().captions).toBe(false);
    media.textTracks.push({ kind: 'captions', mode: 'disabled' });
    f.notify();
    expect(f.store.toggleSubtitles).toHaveBeenLastCalledWith(false);
    expect(media.textTracks[0].mode).toBe('disabled');
  });

  it('leaves captions alone on an audio store with no text-track feature', () => {
    savePlayerPrefs({ volume: 0.5, muted: false, rate: 1, captions: false });
    const media = fakeMedia();
    const f = fakeStore(media, { captions: false });
    bindPlayerPrefs(f.store, asMedia(media));
    f.notify();
    expect(stored()).toEqual({ volume: 0.5, muted: false, rate: 1, captions: false });
  });

  it('stops listening when disposed', () => {
    const media = fakeMedia();
    const f = fakeStore(media, { attached: false });
    const dispose = bindPlayerPrefs(f.store, asMedia(media));
    expect(f.listenerCount()).toBe(1);
    dispose();
    expect(f.listenerCount()).toBe(0);
  });
});
