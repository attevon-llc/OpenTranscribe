/**
 * Reference-counted body scroll lock.
 *
 * Industry-standard pattern for coordinating scroll locking across
 * multiple independently-mounted modals, panels, and dropdowns.
 * Each opener calls lockScroll(); each closer calls unlockScroll().
 * Body only unlocks when the count reaches zero — last one out turns off the light.
 *
 * Why a counter instead of a boolean: multiple components (modals, drawers,
 * dropdowns) can request the lock simultaneously. A simple boolean would unlock
 * the body the moment ANY one component closes, even if others are still open.
 */

let _count = 0;

export function lockScroll(): void {
  _count++;
  if (_count === 1 && typeof document !== 'undefined') {
    document.documentElement.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
  }
}

export function unlockScroll(): void {
  _count = Math.max(0, _count - 1);
  if (_count === 0 && typeof document !== 'undefined') {
    document.documentElement.style.overflow = '';
    document.body.style.overflow = '';
  }
}

/**
 * Emergency reset — zeros the counter and removes all locks.
 * Call on page navigation to guarantee a clean slate if any component
 * failed to call unlockScroll() before being destroyed.
 */
export function resetScrollLock(): void {
  _count = 0;
  if (typeof document !== 'undefined') {
    document.documentElement.style.overflow = '';
    document.body.style.overflow = '';
  }
}
