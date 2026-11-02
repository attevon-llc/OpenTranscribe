const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
// Permissive on purpose, to cover LDAP naming conventions: alphanumerics, dots, underscores,
// hyphens, backslashes (DOMAIN\username) and at signs (user@domain).
const USERNAME_RE = /^[a-zA-Z0-9._\\@-]{2,}$/;

/** Whether `identifier` is an email address or a username (LDAP logins use the same field). */
export function isValidLoginIdentifier(identifier: string): boolean {
  const trimmed = String(identifier).trim();
  return EMAIL_RE.test(trimmed) || USERNAME_RE.test(trimmed);
}

/** The i18n key of why the forced-password-change form cannot be submitted, or null. */
export function forcedChangeError(
  current: string,
  next: string,
  confirm: string
): 'auth.allFieldsRequired' | 'auth.passwordsNoMatch' | null {
  if (!current || !next || !confirm) return 'auth.allFieldsRequired';
  if (next !== confirm) return 'auth.passwordsNoMatch';
  return null;
}
