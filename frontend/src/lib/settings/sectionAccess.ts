/**
 * Which privilege tier each Settings section needs, and the tooltip for one a user cannot open.
 */
import type { SettingsSection } from '$stores/settingsModalStore';

type Translate = (key: string, options?: Record<string, unknown>) => string;

/**
 * The privilege tier each gated section needs, mirroring the FastAPI dependency
 * on the endpoints it calls (`get_current_admin_user` vs
 * `get_current_active_superuser`). This is the ONE place the modal decides who
 * may open what: the sidebar, the mobile picker and the content router all read
 * it, so a nav entry can no longer disagree with what the panel renders.
 *
 * Sections absent from the map are open to any signed-in user. `redaction-policy`
 * and `engine-settings` are deliberately absent: each is now a tab of another section
 * (Privacy & Redaction, Speaker Identification; the ids survive only as deep-link
 * aliases) and `privacyRedactionTabs` / `speakerIdentificationTabs` gate those tabs
 * by the same super_admin tier. `asr-provider`, `custom-vocabulary` and
 * `speaker-attributes` are open to all and likewise only alias a tab.
 */
export const SECTION_MIN_ROLE: Partial<Record<SettingsSection, 'admin' | 'super_admin'>> = {
  // super_admin — deployment configuration (P4.3 moved these off the admin tier)
  authentication: 'super_admin',
  'audit-logs': 'super_admin',
  // admin
  'admin-users': 'admin',
  quarantine: 'admin',
  'platform-holds': 'admin',
  'takedown-notices': 'admin',
  'data-management': 'admin',
  'support-access': 'admin',
};

/**
 * A section the user cannot open is rendered **disabled with a tooltip**, never
 * omitted. Silently dropping the entry is what made an admin conclude the LDAP
 * configuration page did not exist, so absence is no longer used to express
 * "not for you" — only greyed-out presence is.
 *
 * Below-admin users still don't see admin groups at all: those sit inside
 * `isAdmin`-gated sidebar groups, so there is no entry to grey out.
 */
export function sectionLocked(
  section: SettingsSection,
  admin: boolean,
  superAdmin: boolean
): boolean {
  const required = SECTION_MIN_ROLE[section];
  if (required === 'super_admin') return !superAdmin;
  if (required === 'admin') return !admin;
  return false;
}

/** Tooltip for a locked entry, worded for the tier it actually needs. */
export function lockReason(section: SettingsSection, t: Translate): string {
  return SECTION_MIN_ROLE[section] === 'admin'
    ? t('settings.permission.adminTitle')
    : t('settings.nav.requiresSuperAdmin');
}
