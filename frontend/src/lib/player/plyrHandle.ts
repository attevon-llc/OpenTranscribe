/**
 * `PlayerHandle` over Plyr: the only module in the app that imports Plyr (issue #1327).
 *
 * Plyr is deprecated upstream. Every host moved to the seam in `types.ts` first, so the
 * replacement swaps this file and its hosts' one import, nothing else. The structural guard
 * `playerSeam.guard.test.ts` fails on a Plyr import anywhere else. Delete this file (and its
 * test) when the last host has moved off it.
 */
import Plyr from 'plyr';
import 'plyr/dist/plyr.css';
import { applyMediaSeek } from '$lib/utils/mediaReady';
import type { OwnedPlayer, PlayerOptions } from './types';

/** Plyr's own icons and blank video, self-hosted so no third-party origin is ever requested. */
const SELF_HOSTED = {
  iconUrl: '/plyr.svg',
  // Plyr's default points at cdn.plyr.io and is requested by the browser whenever a
  // player is destroyed or its source changes; serve it locally so no third party is hit.
  blankVideo: '/plyr-blank.mp4',
};

const CAPTIONS = { active: true, language: 'auto', update: true };

function isApplePlatform(): boolean {
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
}

/** The Plyr configuration for a host, exactly as each player built it before the seam. */
export function plyrConfig({ variant, audio }: PlayerOptions): Plyr.Options {
  if (variant === 'mini') {
    const controls = audio
      ? ['play', 'current-time', 'duration', 'progress', 'mute', 'volume', 'settings']
      : ['play', 'current-time', 'duration', 'progress', 'mute', 'settings', 'fullscreen'];
    return {
      controls,
      settings: audio ? ['speed'] : ['captions', 'speed'],
      ...SELF_HOSTED,
      keyboard: { global: false },
      tooltips: { controls: true },
      captions: CAPTIONS,
      fullscreen: { iosNative: false, fallback: true },
    };
  }

  // YouTube-style order: volume, then CC to its right, then the rest.
  const controls = ['play-large', 'play', 'current-time', 'duration', 'progress', 'mute', 'volume'];
  if (!audio) controls.push('captions');
  controls.push('settings');
  if (!audio) controls.push('pip');
  if (isApplePlatform()) controls.push('airplay');
  if (!audio) controls.push('fullscreen');

  return {
    controls,
    settings: audio ? ['speed'] : ['captions', 'speed'],
    ...SELF_HOSTED,
    keyboard: { global: true },
    tooltips: { controls: true },
    captions: CAPTIONS,
    // No `playsinline` key: it is Plyr's default (and not in its typings), and the host's
    // element carries the attribute itself.
    fullscreen: { iosNative: true },
  };
}

/** Plyr keeps the real caption state here; its typings do not declare it. */
type PlyrCaptionState = { captions?: { toggled?: boolean } };

/** Build Plyr on `media` and return the seam over it. Throws if Plyr cannot construct. */
export function createPlyrPlayer(media: HTMLMediaElement, options: PlayerOptions): OwnedPlayer {
  const plyr = new Plyr(media, plyrConfig(options));
  if (options.onReady) {
    const onReady = options.onReady;
    plyr.on('ready', () => onReady());
  }

  return {
    media,
    play: () => Promise.resolve(plyr.play()),
    pause: () => {
      plyr.pause();
    },
    seek: (time: number) => {
      // Plyr's setter ignores a seek while its duration is still 0, so before metadata the
      // element is seeked directly; the browser keeps that as the start position.
      if (plyr.duration) {
        plyr.currentTime = time;
      } else {
        applyMediaSeek(media, time);
      }
    },
    setPlaybackRate: (rate: number) => {
      plyr.speed = rate;
    },
    setCaptionsVisible: (visible: boolean) => {
      plyr.toggleCaptions(visible);
    },
    get captionsVisible() {
      return Boolean((plyr as unknown as PlyrCaptionState).captions?.toggled);
    },
    destroy: () => {
      plyr.destroy();
    },
  };
}

/**
 * Whether the viewer switched captions off in an earlier session.
 *
 * Plyr remembers that in `localStorage['plyr']`; the file-detail player turns captions on for
 * everyone else once the track loads.
 */
export function viewerTurnedCaptionsOff(): boolean {
  try {
    return JSON.parse(localStorage.getItem('plyr') || '{}')?.captions === false;
  } catch {
    return false;
  }
}
