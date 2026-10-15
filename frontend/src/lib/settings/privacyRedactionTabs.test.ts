import { describe, it, expect } from 'vitest';
import {
  privacyRedactionTabs,
  privacyRedactionVisible,
  resolvePrivacyRedactionTab,
  type PrivacyRedactionAccess,
} from './privacyRedactionTabs';

const access = (over: Partial<PrivacyRedactionAccess> = {}): PrivacyRedactionAccess => ({
  isAdmin: false,
  isSuperAdmin: false,
  userCap: true,
  policyCap: true,
  ...over,
});

describe('privacyRedactionTabs', () => {
  it('gives a plain user only the personal tab', () => {
    expect(privacyRedactionTabs(access())).toEqual([{ id: 'personal', locked: false }]);
  });

  it('shows an admin the policy tab locked, not omitted', () => {
    expect(privacyRedactionTabs(access({ isAdmin: true }))).toEqual([
      { id: 'personal', locked: false },
      { id: 'policy', locked: true },
    ]);
  });

  it('opens both tabs for a super admin', () => {
    expect(privacyRedactionTabs(access({ isAdmin: true, isSuperAdmin: true }))).toEqual([
      { id: 'personal', locked: false },
      { id: 'policy', locked: false },
    ]);
  });

  it('drops a tab whose capability the deployment lacks', () => {
    const tabs = privacyRedactionTabs(
      access({ isAdmin: true, isSuperAdmin: true, userCap: false })
    );
    expect(tabs).toEqual([{ id: 'policy', locked: false }]);
    expect(privacyRedactionTabs(access({ policyCap: false, isAdmin: true }))).toEqual([
      { id: 'personal', locked: false },
    ]);
  });

  it('hides the whole section when no tab exists', () => {
    expect(privacyRedactionVisible(access({ userCap: false }))).toBe(false);
    expect(privacyRedactionVisible(access())).toBe(true);
  });
});

describe('resolvePrivacyRedactionTab', () => {
  const both = privacyRedactionTabs(access({ isAdmin: true, isSuperAdmin: true }));
  const adminOnly = privacyRedactionTabs(access({ isAdmin: true }));

  it('honours a deep link to the policy tab when it is openable', () => {
    expect(resolvePrivacyRedactionTab('policy', both)).toBe('policy');
  });

  it('falls back to personal when the requested tab is locked', () => {
    expect(resolvePrivacyRedactionTab('policy', adminOnly)).toBe('personal');
  });

  it('falls back to the first tab when everything is locked, so the pane can explain', () => {
    expect(resolvePrivacyRedactionTab('personal', [{ id: 'policy', locked: true }])).toBe('policy');
  });

  it('returns null with no tabs', () => {
    expect(resolvePrivacyRedactionTab('personal', [])).toBeNull();
  });
});
