/**
 * The slice of a Video.js 10 player store (`<video-player>.store` / `<audio-player>.store`)
 * that the app's player modules use, plus the caption-track reads they share.
 *
 * Structural on purpose: unit tests drive a fake, and `videojsStore.test.ts` checks at type
 * level that the real `VideoPlayerElement` and `AudioPlayerElement` stores still satisfy it,
 * so an upstream rename fails `npm run check` instead of production.
 * Store reference: https://videojs.org/docs/framework/html/reference/api/player-store
 *
 * Actions throw until the store has attached to its media (`target` is null before that),
 * per https://videojs.org/docs/framework/html/guides/user-preferences ("Actions need an
 * attached media target"). The text-track members are optional because the audio preset
 * does not include the text-track feature.
 */
export interface VideojsStore {
  readonly target: object | null;
  subscribe(callback: () => void): () => void;
  play(): Promise<void>;
  pause(): void;
  setVolume(volume: number): number;
  setMuted(muted: boolean): boolean;
  setPlaybackRate(rate: number): void;
  readonly toggleSubtitles?: (forceShow?: boolean) => boolean;
}

/** Caption and subtitle tracks of *media* (chapters and metadata tracks excluded). */
export function captionTracks(media: HTMLMediaElement): TextTrack[] {
  const list = media.textTracks;
  if (!list) return [];
  return Array.from(list).filter((t) => t.kind === 'captions' || t.kind === 'subtitles');
}

/**
 * Whether a caption or subtitle track is showing, read from the media element.
 *
 * Read from the element rather than the store's `subtitlesShowing`: the store learns about a
 * mode change from the track list's `change` event, a task later, so reading it right after
 * a restore would report the previous value.
 */
export function captionsShowing(media: HTMLMediaElement): boolean {
  return captionTracks(media).some((t) => t.mode === 'showing');
}
