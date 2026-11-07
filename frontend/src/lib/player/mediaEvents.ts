/**
 * Native media-element events for the player hosts.
 *
 * Playback events stay on the `<video>`/`<audio>` element rather than going through the player
 * library (`types.ts`), so the hosts subscribe here and get one function that removes everything.
 */

export type MediaEventHandlers = Partial<
  Record<keyof HTMLMediaElementEventMap, (event: Event) => void>
>;

/** Add every handler to `media`; the returned function removes exactly those listeners. */
export function listenToMedia(media: HTMLMediaElement, handlers: MediaEventHandlers): () => void {
  const entries = Object.entries(handlers).filter(
    (entry): entry is [string, (event: Event) => void] => typeof entry[1] === 'function'
  );
  for (const [type, handler] of entries) {
    media.addEventListener(type, handler);
  }
  return () => {
    for (const [type, handler] of entries) {
      media.removeEventListener(type, handler);
    }
  };
}

/**
 * The element's duration in seconds, or 0 while it is unknown.
 *
 * `HTMLMediaElement.duration` is `NaN` before metadata and `Infinity` for an unbounded stream;
 * the players report 0 for both (the value Plyr's own `duration` getter returned).
 */
export function mediaDuration(media: HTMLMediaElement): number {
  const { duration } = media;
  return Number.isFinite(duration) ? duration : 0;
}
