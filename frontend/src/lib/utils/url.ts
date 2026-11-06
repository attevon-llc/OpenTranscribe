/**
 * Link to the Flower (Celery monitoring) dashboard, served under a path prefix on the
 * same origin as the app.
 *
 * Always same-origin: the Vite dev proxy and nginx add Flower's basic-auth header on
 * the server side, so the browser never sees or sends Flower credentials.
 * `VITE_FLOWER_URL_PREFIX` must match the backend's `FLOWER_URL_PREFIX`.
 *
 * @returns `<origin>/<prefix>/` (default prefix `flower`), or `''` without a window (SSR).
 */
export function getFlowerUrl(): string {
  if (typeof window === 'undefined') {
    return '';
  }
  const prefix = (import.meta.env.VITE_FLOWER_URL_PREFIX || 'flower').replace(/^\/+|\/+$/g, '');
  return `${window.location.origin}/${prefix}/`;
}
