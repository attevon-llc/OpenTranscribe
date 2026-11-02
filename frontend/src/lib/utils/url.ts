/**
 * Utility functions for constructing URLs based on environment configuration.
 * Handles differences between localhost development (with specific ports) and
 * production deployment (behind Nginx reverse proxy).
 */

/**
 * Constructs the Flower Dashboard URL.
 * Both dev (Vite proxy) and production (Nginx proxy) inject the Basic Auth
 * header server-side, so the browser never needs to handle credentials.
 */
export function getFlowerUrl(): string {
  if (typeof window === 'undefined') return '';

  const urlPrefix = import.meta.env.VITE_FLOWER_URL_PREFIX || 'flower';
  const cleanPrefix = urlPrefix.replace(/^\/+|\/+$/g, '');

  return `${window.location.origin}/${cleanPrefix}/`;
}
