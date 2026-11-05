/**
 * Host identity for protected-media (MediaCMS) sources, shared by every UI host decision.
 */

const DEFAULT_PORTS: Record<string, string> = { 'http:': '80', 'https:': '443' };

// One identity rule, mirrored by `_host_identity` in backend app/utils/media_origin.py:
// lower-cased hostname plus the port, where an absent port means the scheme's default.
export function hostIdentity(rawHost: string, protocol: string): string | null {
  const host = rawHost.trim();
  // The backend compares the raw string; WHATWG URL normalises these (userinfo, backslash,
  // percent-escapes, IDN), so the UI would match a host the server refuses.
  if (/[@\\%]/.test(host) || /[^\x21-\x7e]/.test(host)) return null;
  try {
    const parsed = new URL(`${protocol}//${host}`);
    return `${parsed.hostname.toLowerCase()}:${parsed.port || DEFAULT_PORTS[protocol] || ''}`;
  } catch {
    return null;
  }
}
