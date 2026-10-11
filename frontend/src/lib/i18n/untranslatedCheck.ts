/**
 * `npm run check:i18n` enforces key PARITY, so an English value copied into another locale
 * passes it. This finds those copies (issue #1234): a value in a non-English locale that is
 * identical to English, long enough to be prose, and not allowlisted with a written reason.
 */
export const MIN_PROSE_LENGTH = 25;

/** Keys whose value is legitimately the same in every language, each with the reason. */
export const SAME_IN_EVERY_LANGUAGE: Record<string, string> = {
  'apiDebugger.endpointComments': 'an API path',
  'apiDebugger.endpointCommentsDirect': 'an API path',
  'firstRunWizard.postureDirectoryTitle': 'product and protocol names (Active Directory, LDAP)',
  'settings.emailNotifications.links.recipientsPlaceholder': 'example email addresses',
  'settings.embeddingMigration.v3Description': 'model name and dimension',
  'settings.embeddingMigration.v4Description': 'model name and dimension',
  'settings.groupMappings.claimValuePlaceholderLdap': 'an LDAP distinguished-name example',
  'settings.saml.spAcsUrl': 'standard SAML protocol term',
  'settings.saml.spSlsUrl': 'standard SAML protocol term',
  'settings.speakerIdentification.credential.providerLink':
    'only placeholders and an arrow; the placeholders carry the translated names',
  'userManagement.linkIdentity.providerLdap': 'protocol and attribute names',
};

function looksLikeProse(value: string): boolean {
  return value.length > MIN_PROSE_LENGTH && /[a-z]{4}/.test(value);
}

export function findUntranslated(
  english: Record<string, string>,
  locale: Record<string, string>,
  allowed: Record<string, string> = SAME_IN_EVERY_LANGUAGE
): string[] {
  return Object.entries(english)
    .filter(
      ([key, value]) =>
        typeof value === 'string' &&
        looksLikeProse(value) &&
        locale[key] === value &&
        !(key in allowed)
    )
    .map(([key]) => key);
}
