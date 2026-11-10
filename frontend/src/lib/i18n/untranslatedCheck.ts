/**
 * `npm run check:i18n` enforces key PARITY, so an English value copied into another locale
 * passes it. This finds those copies (issue #1234): a value in a non-English locale that is
 * identical to English, long enough to be prose, and not allowlisted with a written reason.
 */
const MIN_PROSE_LENGTH = 25;

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
  'nav.logoAlt': 'vendor or product name',
  'llm.provider.openai': 'vendor or product name',
  'llm.provider.ollama': 'vendor or product name',
  'llm.provider.claude': 'vendor or product name',
  'llm.provider.anthropic': 'vendor or product name',
  'llm.provider.openrouter': 'vendor or product name',
  'llm.provider.bedrock': 'vendor or product name',
  'settings.authentication.ldap.title': 'protocol, standard or format term',
  'settings.authentication.ldap.bindDn': 'protocol, standard or format term',
  'settings.asrProvider.providers.deepgram': 'vendor or product name',
  'settings.asrProvider.providers.assemblyai': 'vendor or product name',
  'settings.asrProvider.providers.openai': 'vendor or product name',
  'settings.asrProvider.providers.google': 'vendor or product name',
  'settings.asrProvider.providers.azure': 'vendor or product name',
  'settings.asrProvider.providers.aws': 'vendor or product name',
  'settings.asrProvider.providers.speechmatics': 'vendor or product name',
  'settings.asrProvider.providers.gladia': 'vendor or product name',
  'settings.authentication.method.ldap': 'protocol, standard or format term',
  'settings.ldap.bindDn': 'protocol, standard or format term',
  'settings.oidc.oidcTitle': 'protocol, standard or format term',
  'settings.oidc.presetKeycloak': 'vendor or product name',
  'settings.oidc.presetAuthentik': 'vendor or product name',
  'settings.oidc.presetEntra': 'vendor or product name',
  'settings.oidc.presetOkta': 'vendor or product name',
  'settings.oidc.presetGoogle': 'vendor or product name',
  'settings.oidc.realm': 'protocol, standard or format term',
  'settings.contentRedaction.customWordsPlaceholder': 'example input value',
  'settings.contentRedaction.allowlistPlaceholder': 'example input value',
  'settings.groupMappings.claimValuePlaceholderOidc': 'example input value',
  'settings.groupMappings.test.usernamePlaceholder': 'example input value',
  'chat.trace.source.postgres': 'vendor or product name',
  'chat.trace.source.opensearch': 'vendor or product name',
  'documentExport.pageSizeLetter': 'protocol, standard or format term',
  'transcript.exportMarkdown': 'protocol, standard or format term',
  'settings.authentication.oidc.realm': 'protocol, standard or format term',
  'userManagement.linkIdentity.identifierLdap': 'protocol, standard or format term',
  'userManagement.linkIdentity.identifierOidc': 'protocol, standard or format term',
  'search.speakerOperatorExample': 'API path or search-operator example',
  'settings.auditLog.userAgent': 'protocol, standard or format term',
};

/**
 * Locales whose script differs from English. An English word is never a cognate there, so
 * ANY identical Latin word is an untranslated string (or a brand/term that must be allowlisted).
 * The Latin-script locales share cognates with English ("Status", "Menu", "Collections"), so
 * only prose-length copies are flagged for them.
 */
export const NON_LATIN_LOCALES: readonly string[] = ['ar', 'ru', 'ja', 'ko', 'zh'];

function looksLikeProse(value: string): boolean {
  return value.length > MIN_PROSE_LENGTH && /[a-z]{4}/.test(value);
}

function hasLatinWord(value: string): boolean {
  return /[a-z]{3}/.test(value.replace(/\{\{[^}]*\}\}/g, ''));
}

export function findUntranslated(
  english: Record<string, string>,
  locale: Record<string, string>,
  allowed: Record<string, string> = SAME_IN_EVERY_LANGUAGE,
  strict = false
): string[] {
  return Object.entries(english)
    .filter(
      ([key, value]) =>
        typeof value === 'string' &&
        (strict ? hasLatinWord(value) : looksLikeProse(value)) &&
        locale[key] === value &&
        !(key in allowed)
    )
    .map(([key]) => key);
}
