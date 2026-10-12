/**
 * Which tabs the merged "Privacy & Redaction" settings section offers a user.
 *
 * The backend is the authority (`/user-settings/redaction` is per-user,
 * `/admin/redaction-policy` is `get_current_active_superuser`); this only decides
 * what is worth rendering. A tab the user's tier cannot open is returned `locked`
 * rather than dropped, matching the Settings convention that a privilege the user
 * merely lacks is shown greyed out. A tab the user could never reach at all — the
 * policy tab for a plain user — is absent, as is one whose capability the
 * deployment does not have.
 */

export type PrivacyRedactionTabId = 'personal' | 'policy';

export interface PrivacyRedactionTab {
  id: PrivacyRedactionTabId;
  locked: boolean;
}

export interface PrivacyRedactionAccess {
  isAdmin: boolean;
  isSuperAdmin: boolean;
  /** `redaction.user` capability. */
  userCap: boolean;
  /** `redaction.policy` capability. */
  policyCap: boolean;
}

export function privacyRedactionTabs(access: PrivacyRedactionAccess): PrivacyRedactionTab[] {
  const tabs: PrivacyRedactionTab[] = [];
  if (access.userCap) tabs.push({ id: 'personal', locked: false });
  if (access.isAdmin && access.policyCap) {
    tabs.push({ id: 'policy', locked: !access.isSuperAdmin });
  }
  return tabs;
}

/** Whether the sidebar should list the section at all. */
export function privacyRedactionVisible(access: PrivacyRedactionAccess): boolean {
  return privacyRedactionTabs(access).length > 0;
}

/**
 * The tab to show for a requested one: the request when it is openable, otherwise
 * the first openable tab, otherwise (every tab locked) the first tab so the pane
 * can explain the lock instead of rendering blank.
 */
export function resolvePrivacyRedactionTab(
  requested: PrivacyRedactionTabId,
  tabs: PrivacyRedactionTab[]
): PrivacyRedactionTabId | null {
  const wanted = tabs.find((tab) => tab.id === requested && !tab.locked);
  if (wanted) return wanted.id;
  return (tabs.find((tab) => !tab.locked) ?? tabs[0])?.id ?? null;
}
