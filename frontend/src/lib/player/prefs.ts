/**
 * Remembered player preferences: volume, mute, speed and captions on/off.
 *
 * Video.js 10 deliberately leaves persistence to the app ("Video.js does not write
 * preferences to storage"); this follows its documented restore-then-save pattern:
 * https://videojs.org/docs/framework/html/guides/user-preferences
 * and closes the gap its Plyr guide lists ("Persist preferences in your app"):
 * https://videojs.org/docs/framework/html/guides/migrate-from-plyr
 *
 * Captions default to ON (plan #1327 row 23): they turn on unless the viewer switched them
 * off. A viewer's choices made under Plyr (`localStorage['plyr']`) are carried over once.
 * Storage can be missing or throw (private modes, embedded contexts), so every access is
 * guarded and playback never depends on it.
 */
import { captionTracks, captionsShowing, type VideojsStore } from './videojsStore';

export const PLAYER_PREFS_KEY = 'opentr:playerPrefs';
/** Plyr's own storage key (`plyr/src/js/storage.js`), read once to migrate. */
export const LEGACY_PLYR_KEY = 'plyr';

export interface PlayerPrefs {
  /** 0 to 1. */
  volume: number;
  muted: boolean;
  rate: number;
  captions: boolean;
}

export const DEFAULT_PLAYER_PREFS: Readonly<PlayerPrefs> = Object.freeze({
  volume: 1,
  muted: false,
  rate: 1,
  captions: true,
});

/** Browsers play 0.0625x to 16x; anything outside is not a real preference. */
const MIN_RATE = 0.0625;
const MAX_RATE = 16;

function defaultStorage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null; // the accessor itself throws SecurityError in some sandboxed frames
  }
}

function readJson(storage: Storage | null, key: string): Record<string, unknown> | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(key);
    if (raw === null) return null;
    const value: unknown = JSON.parse(raw);
    return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** Keep each valid field; any missing or invalid one falls back to its default. */
function sanitize(raw: Record<string, unknown>, rateKey: 'rate' | 'speed'): PlayerPrefs {
  const { volume, muted, captions } = raw;
  const rate = raw[rateKey];
  const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
  return {
    volume: isNum(volume) && volume >= 0 && volume <= 1 ? volume : DEFAULT_PLAYER_PREFS.volume,
    muted: typeof muted === 'boolean' ? muted : DEFAULT_PLAYER_PREFS.muted,
    rate: isNum(rate) && rate >= MIN_RATE && rate <= MAX_RATE ? rate : DEFAULT_PLAYER_PREFS.rate,
    captions: typeof captions === 'boolean' ? captions : DEFAULT_PLAYER_PREFS.captions,
  };
}

export function savePlayerPrefs(prefs: PlayerPrefs, storage: Storage | null = defaultStorage()) {
  if (!storage) return;
  try {
    storage.setItem(PLAYER_PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // Storage full or blocked: the preference is simply not remembered.
  }
}

export function loadPlayerPrefs(storage: Storage | null = defaultStorage()): PlayerPrefs {
  const current = readJson(storage, PLAYER_PREFS_KEY);
  if (current) return sanitize(current, 'rate');
  const legacy = readJson(storage, LEGACY_PLYR_KEY);
  if (!legacy) return { ...DEFAULT_PLAYER_PREFS };
  const migrated = sanitize(legacy, 'speed');
  savePlayerPrefs(migrated, storage);
  return migrated;
}

function readMedia(media: HTMLMediaElement, captions: boolean): PlayerPrefs {
  return { volume: media.volume, muted: media.muted, rate: media.playbackRate, captions };
}

/**
 * Restore the remembered preferences into *store* once it attaches, then save the viewer's
 * later changes. Returns the unsubscribe function.
 *
 * Values are read from *media*, not from store state: the store learns about a rate or
 * caption-mode change from a media event a task later, so right after a restore it still
 * reports the old value and saving it would overwrite the preference just restored.
 *
 * Captions are restored each time a caption track list appears (the app swaps its blob-URL
 * track after an edit), and their state is saved only while a track exists, as the Video.js
 * guide's "Persist caption preference" variation describes.
 */
export function bindPlayerPrefs(
  store: VideojsStore,
  media: HTMLMediaElement,
  storage: Storage | null = defaultStorage()
): () => void {
  let saved = loadPlayerPrefs(storage);
  let attached = false;
  let hadTracks = false;

  const sync = () => {
    if (!store.target) return;
    if (!attached) {
      attached = true;
      store.setVolume(saved.volume);
      if (saved.muted) store.setMuted(true); // after setVolume, which unmutes above zero
      if (saved.rate !== media.playbackRate) store.setPlaybackRate(saved.rate);
    }

    const hasTracks = store.toggleSubtitles !== undefined && captionTracks(media).length > 0;
    if (hasTracks && !hadTracks) store.toggleSubtitles?.(saved.captions);
    hadTracks = hasTracks;

    const next = readMedia(media, hasTracks ? captionsShowing(media) : saved.captions);
    if (
      next.volume !== saved.volume ||
      next.muted !== saved.muted ||
      next.rate !== saved.rate ||
      next.captions !== saved.captions
    ) {
      saved = next;
      savePlayerPrefs(saved, storage);
    }
  };

  const unsubscribe = store.subscribe(sync);
  sync();
  return unsubscribe;
}
