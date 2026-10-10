/**
 * The `X-Support-Access-Grant` request header (issue #1122).
 *
 * Deliberately imports nothing from `$stores` or `$lib/api`, so `$lib/axios` and
 * `thumbnailCache` can depend on it without an import cycle. The active grant is set only
 * by `$stores/supportSession`.
 */

export const SUPPORT_GRANT_HEADER = 'X-Support-Access-Grant';

let activeGrantUuid: string | null = null;

export function setActiveSupportGrant(uuid: string | null): void {
  activeGrantUuid = uuid;
}

/**
 * Path prefixes (relative to `/api`) that never carry the header.
 *
 * The backend ignores the header on the first five (a stale header must not be able to
 * block the very call that ends the session), chat is owner-only and refuses grants, and
 * `/admin` is deployment administration, which a grant never reaches: sending it there
 * would only turn every admin call into a 403.
 */
const EXEMPT_PREFIXES = [
  '/auth/',
  '/system/',
  '/support-access/',
  '/org-admin/',
  '/users/me/support-access',
  '/chat',
  '/admin/',
];

function isAbsoluteUrl(url: string): boolean {
  return /^([a-z][a-z0-9+.-]*:)?\/\//i.test(url) || /^[a-z][a-z0-9+.-]*:/i.test(url);
}

function relativePath(url: string): string {
  const path = url.startsWith('/api/') ? url.slice(4) : url;
  return path.startsWith('/') ? path : `/${path}`;
}

function matchesPrefix(path: string, prefix: string): boolean {
  if (prefix.endsWith('/')) return path.startsWith(prefix) || path === prefix.slice(0, -1);
  return path === prefix || path.startsWith(`${prefix}/`) || path.startsWith(`${prefix}?`);
}

/** True when `url` is a lifecycle, auth, system, chat or admin route (header never sent). */
export function isSupportAccessExempt(url: string): boolean {
  const path = relativePath(url);
  return EXEMPT_PREFIXES.some((prefix) => matchesPrefix(path, prefix));
}

/**
 * The header to attach to a request for `url`: `{}` when no session is active, the URL is
 * missing, absolute (presigned object-storage URLs must never see the grant uuid) or exempt.
 */
export function getSupportAccessHeaders(url: string | undefined): Record<string, string> {
  if (!activeGrantUuid || !url || isAbsoluteUrl(url) || isSupportAccessExempt(url)) return {};
  return { [SUPPORT_GRANT_HEADER]: activeGrantUuid };
}
