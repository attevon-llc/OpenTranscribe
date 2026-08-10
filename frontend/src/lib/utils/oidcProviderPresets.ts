/**
 * OIDC provider presets — fills the claim path, scopes, and discovery URL shape
 * for a known identity provider so an admin does not have to already know that
 * Keycloak nests roles under `realm_access.roles` and Authentik does not.
 *
 * Pure data + pure functions. `OIDCSettings.svelte` is the only caller; selecting
 * a preset mutates the same real, saved form fields an admin would otherwise type
 * by hand — there is no separate "preset" setting persisted anywhere.
 */

export interface OIDCProviderPreset {
  id: string;
  labelKey: string;
  rolesClaim: string;
  scopes: string;
  /** Discovery URL to prefill, with a placeholder segment the admin must replace
   *  (tenant ID, app slug, Okta domain). Empty for providers resolved via the
   *  realm-based fallback (Keycloak) or with no known shape (Generic). */
  discoveryUrlTemplate: string;
  /** i18n key for a provider-specific caveat shown once selected. */
  noteKey?: string;
}

export const OIDC_PROVIDER_PRESETS: readonly OIDCProviderPreset[] = [
  {
    id: 'generic',
    labelKey: 'settings.oidc.presetGeneric',
    rolesClaim: 'realm_access.roles',
    scopes: 'openid email profile',
    discoveryUrlTemplate: '',
  },
  {
    id: 'keycloak',
    labelKey: 'settings.oidc.presetKeycloak',
    rolesClaim: 'realm_access.roles',
    scopes: 'openid email profile',
    discoveryUrlTemplate: '',
  },
  {
    id: 'authentik',
    labelKey: 'settings.oidc.presetAuthentik',
    rolesClaim: 'groups',
    scopes: 'openid email profile',
    discoveryUrlTemplate:
      'https://authentik.example.com/application/o/<app-slug>/.well-known/openid-configuration',
    noteKey: 'settings.oidc.presetNoteAuthentik',
  },
  {
    id: 'entra',
    labelKey: 'settings.oidc.presetEntra',
    rolesClaim: 'roles',
    scopes: 'openid email profile',
    discoveryUrlTemplate:
      'https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration',
    noteKey: 'settings.oidc.presetNoteEntra',
  },
  {
    id: 'okta',
    labelKey: 'settings.oidc.presetOkta',
    rolesClaim: 'groups',
    scopes: 'openid email profile groups',
    discoveryUrlTemplate: 'https://<okta-domain>/.well-known/openid-configuration',
    noteKey: 'settings.oidc.presetNoteOkta',
  },
  {
    id: 'google',
    labelKey: 'settings.oidc.presetGoogle',
    rolesClaim: '',
    scopes: 'openid email profile',
    discoveryUrlTemplate: 'https://accounts.google.com/.well-known/openid-configuration',
    noteKey: 'settings.oidc.presetNoteGoogle',
  },
] as const;

export function findOIDCPreset(id: string): OIDCProviderPreset | undefined {
  return OIDC_PROVIDER_PRESETS.find((preset) => preset.id === id);
}

/**
 * The field values a preset fills. Keycloak and Generic leave the discovery URL
 * untouched — Keycloak resolves via the realm-based fallback, Generic has no
 * known shape — every other preset replaces it with a templated URL the admin
 * still has to edit (tenant ID, app slug, or domain).
 */
export function oidcPresetFieldValues(preset: OIDCProviderPreset): {
  oidc_roles_claim: string;
  oidc_scopes: string;
  oidc_discovery_url?: string;
} {
  const values: { oidc_roles_claim: string; oidc_scopes: string; oidc_discovery_url?: string } = {
    oidc_roles_claim: preset.rolesClaim,
    oidc_scopes: preset.scopes,
  };
  if (preset.discoveryUrlTemplate) {
    values.oidc_discovery_url = preset.discoveryUrlTemplate;
  }
  return values;
}
