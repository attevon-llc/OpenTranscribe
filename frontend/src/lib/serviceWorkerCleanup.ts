/**
 * Service-worker teardown.
 *
 * OpenTranscribe deliberately ships **no** service worker. It is an authenticated,
 * always-online SPA: every screen needs the API (httpOnly session cookie + WebSocket),
 * so there is no offline experience for a worker to enable. Chrome dropped the
 * service-worker requirement for PWA installability (M108 mobile / M112 desktop), so
 * the manifest alone keeps "add to home screen" working.
 *
 * This module exists only to *shed* a worker that a browser may still hold from an
 * older build, along with the caches it created. Cache Storage is not covered by
 * `$lib/session/clearUserState`, so a stale cache-first worker could keep serving one
 * user's app shell — and any same-origin `/s3/` media it had cached — after logout.
 */

/** Cache Storage prefix used by the removed build-time service worker. */
const LEGACY_CACHE_PREFIX = 'transcribe-app-cache-';

/**
 * Unregister every service worker on this origin and delete its caches.
 *
 * Safe to call on every startup: it resolves immediately when nothing is registered
 * and swallows all errors, so it can never block or break app boot.
 */
export async function unregisterServiceWorkers(): Promise<void> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;

  try {
    // getRegistrations() resolves with [] when nothing is registered. Never use
    // `navigator.serviceWorker.ready` here — it stays pending forever on an origin
    // that has no active worker.
    const registrations = await navigator.serviceWorker.getRegistrations();
    await Promise.all(registrations.map((registration) => registration.unregister()));
  } catch (error) {
    console.warn('[serviceWorker] Failed to unregister existing workers:', error);
  }

  if (typeof caches === 'undefined') return;

  try {
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((name) => name.startsWith(LEGACY_CACHE_PREFIX))
        .map((name) => caches.delete(name))
    );
  } catch (error) {
    console.warn('[serviceWorker] Failed to purge legacy caches:', error);
  }
}
