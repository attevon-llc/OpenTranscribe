/**
 * The player seam (issue #1327).
 *
 * The app asks a player for imperative control through `PlayerHandle` and nothing else, so the
 * library behind it can be replaced without touching the routes or the components that host it.
 * Playback events are NOT part of the seam: listen on `media` (native `HTMLMediaElement` events),
 * which is what every library we would adopt keeps working.
 */
export interface PlayerHandle {
  readonly media: HTMLMediaElement;
  play(): Promise<void>;
  pause(): void;
  /** Seek the media element now. Pre-metadata safe (see `$lib/utils/mediaReady`). */
  seek(time: number): void;
  setPlaybackRate(rate: number): void;
  setCaptionsVisible(visible: boolean): void;
  readonly captionsVisible: boolean;
}

/**
 * A handle owned by the component that created it. Only the owner tears the player down;
 * everything it hands out (`getPlayer()`) is the narrower `PlayerHandle`.
 */
export interface OwnedPlayer extends PlayerHandle {
  destroy(): void;
}

/**
 * Which player chrome a host wants, in library-neutral terms.
 *
 * - `full`: the file-detail player (page-wide shortcuts, captions, PiP, AirPlay on Apple).
 * - `mini`: the floating search / speaker preview (focus-scoped shortcuts, full-window fallback).
 */
export interface PlayerOptions {
  variant: 'full' | 'mini';
  /** True when the element is an `<audio>`: no captions, PiP or fullscreen controls. */
  audio: boolean;
  /** Called once the library has finished building its controls. */
  onReady?: () => void;
}
