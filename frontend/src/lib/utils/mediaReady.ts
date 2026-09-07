/**
 * Media-element readiness helpers shared by the players.
 *
 * Both `VideoPlayer.svelte` and `PlyrMiniPlayer.svelte` need the same thing: a
 * seek requested before `loadedmetadata` has fired must still land on the right
 * timestamp. They used to carry byte-identical copies of the wait below.
 */

/** `HTMLMediaElement.HAVE_METADATA` — duration and seek points are known. */
export const HAVE_METADATA = 1;

/**
 * How long to keep waiting for `loadedmetadata` before giving up on syncing
 * Plyr's own clock. This is NOT the seek latency: `applyMediaSeek` issues the
 * seek to the media element immediately, so the user's playhead moves without
 * waiting for this. Reaching the timeout only means Plyr's progress bar is
 * re-synced late.
 */
export const METADATA_WAIT_TIMEOUT_MS = 15000;

/**
 * Resolve once the element has metadata.
 *
 * Resolves `true` when `readyState >= HAVE_METADATA`, `false` if the element
 * errored or the timeout elapsed first. Never rejects, and always removes every
 * listener and clears the timer — the previous inline copies leaked their
 * 15-second timer on the happy path, so a seek left a stray callback behind.
 */
export function waitForMediaMetadata(
  media: HTMLMediaElement,
  timeoutMs: number = METADATA_WAIT_TIMEOUT_MS
): Promise<boolean> {
  if (media.readyState >= HAVE_METADATA) {
    return Promise.resolve(true);
  }

  return new Promise<boolean>((resolve) => {
    let timer: ReturnType<typeof setTimeout> | null = null;

    const settle = (ready: boolean) => {
      media.removeEventListener('loadedmetadata', onReady);
      media.removeEventListener('canplay', onReady);
      media.removeEventListener('durationchange', onReady);
      media.removeEventListener('error', onError);
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      resolve(ready);
    };

    const onReady = () => settle(true);
    const onError = () => settle(false);

    media.addEventListener('loadedmetadata', onReady);
    media.addEventListener('canplay', onReady);
    media.addEventListener('durationchange', onReady);
    media.addEventListener('error', onError);

    // The element can cross the threshold between the guard above and the
    // listeners being attached.
    if (media.readyState >= HAVE_METADATA) {
      settle(true);
      return;
    }

    timer = setTimeout(() => settle(false), timeoutMs);
  });
}

/**
 * Seek a media element as early as the browser allows.
 *
 * Assigning `currentTime` while `readyState` is `HAVE_NOTHING` is deliberately
 * NOT skipped: per the HTML spec the element records the value as its *default
 * playback start position*, so the browser issues the range request for the
 * target as soon as it has parsed the container index. Blocking on
 * `loadedmetadata` first — which is what the players used to do — only delays
 * that request; it does not make it more correct.
 *
 * Returns `false` if the assignment threw (some engines raise `InvalidStateError`
 * on a pre-metadata seek), in which case the caller should re-apply once
 * metadata arrives.
 */
export function applyMediaSeek(media: HTMLMediaElement, time: number): boolean {
  try {
    media.currentTime = time;
    return true;
  } catch {
    return false;
  }
}
