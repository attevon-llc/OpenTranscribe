/**
 * Full-window fallback for the mini player (plan #1327 row 28).
 *
 * Where the browser has no element Fullscreen API (iPhone Safari, a frame without
 * `allow="fullscreen"`), Video.js falls back to the video's native presentation, which drops
 * our controls, or hides the button altogether
 * (https://videojs.org/docs/framework/html/guides/fullscreen, "Availability and constraints").
 * Plyr's mini player filled the window instead (`fullscreen.fallback`), and the Video.js Plyr
 * guide lists that as having no matching feature
 * (https://videojs.org/docs/framework/html/guides/migrate-from-plyr), so the host keeps it: a
 * CSS class fills the viewport, this module decides when to use it and owns the keys.
 */
import { isEditableTarget } from './hotkeys';

type FullscreenDocument = Pick<Document, 'fullscreenEnabled'> & {
  webkitFullscreenEnabled?: boolean;
};

/** Whether *doc* can put an element fullscreen. Plyr's own test (`fullscreen.js`, `nativeSupported`). */
export function supportsElementFullscreen(doc: FullscreenDocument = document): boolean {
  return Boolean(doc.fullscreenEnabled || doc.webkitFullscreenEnabled);
}

interface FullWindowKeys {
  /** Whether the player fills the window now. */
  isOn(): boolean;
  set(on: boolean): void;
}

/**
 * `F` toggles full-window inside *container* (the mini player's shortcuts are scoped to it),
 * and Escape leaves it from anywhere on the page. Typing in a field is never a shortcut.
 * Returns the cleanup function.
 */
export function attachFullWindowKeys(container: EventTarget, keys: FullWindowKeys): () => void {
  const onContainerKey = (event: Event) => {
    if (!(event instanceof KeyboardEvent) || event.defaultPrevented || event.repeat) return;
    if (event.key.toLowerCase() !== 'f') return;
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    if (isEditableTarget(event.target)) return;
    event.preventDefault();
    keys.set(!keys.isOn());
  };
  const onWindowKey = (event: Event) => {
    if (!(event instanceof KeyboardEvent) || event.key !== 'Escape' || !keys.isOn()) return;
    event.preventDefault();
    keys.set(false);
  };
  container.addEventListener('keydown', onContainerKey);
  window.addEventListener('keydown', onWindowKey);
  return () => {
    container.removeEventListener('keydown', onContainerKey);
    window.removeEventListener('keydown', onWindowKey);
  };
}
