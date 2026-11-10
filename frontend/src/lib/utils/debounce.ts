/**
 * Creates a debounced function that delays invoking the callback until after
 * `delay` milliseconds have elapsed since the last invocation.
 *
 * Returns an object with `trigger()` to invoke and `cleanup()` to cancel.
 */
export function createDebouncedHandler(
  callback: () => void | Promise<void>,
  delay: number = 300
): { trigger: () => void; cleanup: () => void } {
  let timer: ReturnType<typeof setTimeout> | null = null;

  return {
    trigger() {
      if (timer) clearTimeout(timer);
      // A callback that rejects has nowhere to report to from a timer; log it rather than
      // leaving an unhandled rejection.
      timer = setTimeout(() => {
        Promise.resolve(callback()).catch((err: unknown) =>
          console.error('Debounced callback failed:', err)
        );
      }, delay);
    },
    cleanup() {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    },
  };
}
