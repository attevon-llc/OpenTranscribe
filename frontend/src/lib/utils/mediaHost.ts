/**
 * Host identity for protected-media (MediaCMS) sources, shared by every UI host decision.
 */

const DEFAULT_PORTS: Record<string, string> = { 'http:': '80', 'https:': '443' };

// One identity rule, mirrored by `_host_identity` in backend app/utils/media_origin.py:
// lower-cased hostname plus the port, where an absent port means the scheme's default.
export function hostIdentity(rawHost: string, protocol: string): string | null {
  const host = rawHost.trim();
  // The backend compares the raw string; WHATWG URL normalises these (userinfo, backslash,
  // percent-escapes, IDN, path), so the UI would match a host the server refuses.
  if (/[@\\%/?#]/.test(host) || /[^\x21-\x7e]/.test(host)) return null;
  try {
    const parsed = new URL(`${protocol}//${host}`);
    return `${parsed.hostname.toLowerCase()}:${parsed.port || DEFAULT_PORTS[protocol] || ''}`;
  } catch {
    return null;
  }
}

/**
 * The origin of a configured source as `protocol//host:port`, or null when unusable.
 *
 * A source is stored as `host[:port]` or `scheme://host[:port]`; without a scheme it means
 * https, so its credentials are never offered to a plain-http URL. Mirrors
 * `source_origin` in backend app/utils/media_origin.py.
 */
export function sourceOrigin(configured: string): string | null {
  let value = configured.trim();
  let protocol = 'https:';
  const sep = value.indexOf('://');
  if (sep !== -1) {
    protocol = `${value.slice(0, sep).toLowerCase()}:`;
    value = value.slice(sep + 3);
  }
  if (!(protocol in DEFAULT_PORTS)) return null;
  const identity = hostIdentity(value, protocol);
  return identity === null ? null : `${protocol}//${identity}`;
}

/** True when a configured source names plain `http://`, so credentials cross the network unencrypted. */
export function isPlainHttpSource(configured: string): boolean {
  return /^\s*http:\/\//i.test(configured);
}
