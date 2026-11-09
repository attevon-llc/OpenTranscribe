/**
 * Scroll behaviour of the gallery list: remembering the offset for back-navigation, restoring it,
 * and the infinite-scroll sentinel observer.
 */
import { tick } from 'svelte';

/** Save the scroll offset at most once per animation frame; `cancel` drops a pending save. */
export function createScrollSaver(getEl: () => HTMLElement | null, save: (top: number) => void) {
  let raf: number | null = null;
  return {
    onScroll() {
      if (raf !== null) return;
      raf = requestAnimationFrame(() => {
        raf = null;
        const el = getEl();
        if (el) save(el.scrollTop);
      });
    },
    cancel() {
      if (raf !== null) cancelAnimationFrame(raf);
      raf = null;
    },
  };
}

/**
 * Restore a saved scroll offset after back-navigation. The virtual grid computes its full height
 * a frame or more after mount, so retry until the scroll takes effect (30 attempts at most).
 */
export function restoreScrollTop(getEl: () => HTMLElement | null, savedTop: number): void {
  let attempts = 0;
  const tryRestore = () => {
    const el = getEl();
    if (!el || attempts > 30) return;
    attempts++;
    el.scrollTop = savedTop;
    if (Math.abs(el.scrollTop - savedTop) > 10) requestAnimationFrame(tryRestore);
  };
  void tick().then(() => requestAnimationFrame(tryRestore));
}

/** Observer that calls `onNearEnd` when the sentinel comes within 200px of the viewport. */
export function createInfiniteScrollObserver(onNearEnd: () => void): IntersectionObserver {
  return new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) onNearEnd();
      });
    },
    { root: null, rootMargin: '200px', threshold: 0.1 }
  );
}
