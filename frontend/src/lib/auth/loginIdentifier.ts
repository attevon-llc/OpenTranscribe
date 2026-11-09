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

export interface LoginFieldError {
  messageKey: 'auth.identifierRequired' | 'auth.validIdentifierRequired' | 'auth.passwordRequired';
  /** The input to focus. */
  fieldId: 'email' | 'password';
}

/** The first thing wrong with the sign-in form, checked in field order, or null. */
export function loginFieldError(identifier: string, password: string): LoginFieldError | null {
  if (!identifier.trim()) return { messageKey: 'auth.identifierRequired', fieldId: 'email' };
  if (!isValidLoginIdentifier(identifier.trim())) {
    return { messageKey: 'auth.validIdentifierRequired', fieldId: 'email' };
  }
  if (!password.trim()) return { messageKey: 'auth.passwordRequired', fieldId: 'password' };
  return null;
}

/**
 * Which input to focus after a rejected sign-in, from the HTTP status — never from the message
 * text, which is localised, so matching English substrings silently no-ops in other locales.
 * 401/403 = the credentials were rejected; 400/422 = the identifier itself was malformed.
 */
export function failureFocusField(status: number | undefined): 'email' | 'password' | null {
  if (status === 401 || status === 403) return 'password';
  if (status === 400 || status === 422) return 'email';
  return null;
}
