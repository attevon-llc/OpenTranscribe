/**
 * Validate the IdP authorization URL before the browser is sent to it.
 *
 * Defense-in-depth: even though this comes from our own backend, a misconfiguration or
 * upstream compromise should not turn into an open redirect or javascript:/data: URL
 * injection. Only http(s) URLs are followed.
 */
export type AuthorizationUrlCheck =
  | { ok: true; url: string }
  | { ok: false; reason: 'invalid' }
  | { ok: false; reason: 'protocol'; protocol: string };

export function checkAuthorizationUrl(authorizationUrl: string): AuthorizationUrlCheck {
  let parsed: URL;
  try {
    parsed = new URL(authorizationUrl);
  } catch {
    return { ok: false, reason: 'invalid' };
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    return { ok: false, reason: 'protocol', protocol: parsed.protocol };
  }
  return { ok: true, url: parsed.toString() };
}
