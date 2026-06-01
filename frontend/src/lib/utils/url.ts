/**
 * Utility functions for constructing URLs based on environment configuration.
 * Handles differences between localhost development (with specific ports) and
 * production deployment (behind Nginx reverse proxy).
 */

/**
 * Gets the base URL for the application API.
 * In dev mode with VITE_API_BASE_URL set, it uses that (e.g. http://localhost:5174).
 * In production, it uses the current window origin.
 */
export function getAppBaseUrl(): string {
  if (typeof window === 'undefined') return '';

  const viteApiBaseUrl = import.meta.env.VITE_API_BASE_URL;
  if (viteApiBaseUrl) {
    // Remove /api suffix if present to get clean host
    return viteApiBaseUrl.replace(/\/api\/?$/, '');
  }

  // Production/nginx mode: use current location without port
  return window.location.origin;
}

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
