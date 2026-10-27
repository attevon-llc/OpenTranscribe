/**
 * View rules and copy of the privacy controls (issue #1280).
 *
 * The visibility rule is the security-relevant one: the organization panel must stay hidden
 * unless ALL of (managed edition, capability granted, audience org_admin, person is org:admin)
 * hold. The copy checks guard the translation side: a placeholder dropped in one locale prints
 * a raw `{{word}}` or loses the typed confirmation word.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect } from 'vitest';
import {
  LOCKED_BY_KEYS,
  isLockedAboveOrganization,
  isLockedAboveUser,
  orgPrivacyVisible,
  privacyErrorCode,
  privacyErrorKey,
  purgeCountTotal,
} from './privacyPolicy';

const ALLOWED = {
  isCloudEdition: true,
  capabilityEnabled: true,
  audience: 'org_admin',
  orgRole: 'org:admin',
};

describe('orgPrivacyVisible', () => {
  it('is visible only when every condition holds', () => {
    expect(orgPrivacyVisible(ALLOWED)).toBe(true);
  });

  it.each([
    ['the community edition', { isCloudEdition: false }],
    ['the capability is absent', { capabilityEnabled: false }],
    ['the audience is members, not administrators', { audience: 'member' }],
    ['the audience is unknown', { audience: undefined }],
    ['the person is an organization member', { orgRole: 'org:member' }],
    ['the person has no organization role', { orgRole: undefined }],
  ])('is hidden when %s', (_name, override) => {
    expect(orgPrivacyVisible({ ...ALLOWED, ...override })).toBe(false);
  });
});

describe('lock layers', () => {
  it('an organization cannot override the platform or the deployment, but can its own', () => {
    expect(isLockedAboveOrganization('platform')).toBe(true);
    expect(isLockedAboveOrganization('instance')).toBe(true);
    expect(isLockedAboveOrganization('organization')).toBe(false);
    expect(isLockedAboveOrganization(null)).toBe(false);
  });

  it('a user cannot override any layer above the user', () => {
    expect(
      (['platform', 'instance', 'organization'] as const).every((l) => isLockedAboveUser(l))
    ).toBe(true);
    expect(isLockedAboveUser('user')).toBe(false);
    expect(isLockedAboveUser('consent')).toBe(false);
    expect(isLockedAboveUser(undefined)).toBe(false);
  });
});

describe('error mapping', () => {
  const body = (code: unknown) => ({ response: { data: { detail: { code } } } });

  it('maps the documented codes to their sentence and anything else to the fallback', () => {
    expect(privacyErrorKey(body('policy_still_enabled'), 'x')).toBe('privacy.error.stillEnabled');
    expect(privacyErrorKey(body('policy_locked'), 'x')).toBe('privacy.error.policyLocked');
    expect(privacyErrorKey(body('purge_scope_not_allowed'), 'x')).toBe(
      'privacy.error.scopeNotAllowed'
    );
    expect(privacyErrorKey(body('something_new'), 'fallback.key')).toBe('fallback.key');
    expect(privacyErrorKey(new Error('boom'), 'fallback.key')).toBe('fallback.key');
  });

  it('reads a code only when detail is an object carrying a string code', () => {
    expect(privacyErrorCode({ response: { data: { detail: 'plain text' } } })).toBeUndefined();
    expect(privacyErrorCode(body(42))).toBeUndefined();
    expect(privacyErrorCode(body('policy_locked'))).toBe('policy_locked');
  });

  it('sums purge counts and ignores a missing report', () => {
    expect(purgeCountTotal({ speakers: 3, profiles: 2 })).toBe(5);
    expect(purgeCountTotal(null)).toBe(0);
  });
});

describe('privacy.* copy in every locale', () => {
  const dir = join(__dirname, '..', 'i18n', 'locales');
  const files = readdirSync(dir).filter((f) => f.endsWith('.json'));
  const load = (f: string) =>
    JSON.parse(readFileSync(join(dir, f), 'utf8')) as Record<string, string>;
  const en = load('en.json');
  const keys = Object.keys(en).filter((k) => k.startsWith('privacy.'));
  const placeholders = (s: string) => [...s.matchAll(/\{\{(\w+)\}\}/g)].map((m) => m[1]).sort();

  it('has the keys the code maps to', () => {
    const mapped = [
      ...Object.values(LOCKED_BY_KEYS),
      'privacy.error.policyLocked',
      'privacy.error.stillEnabled',
      'privacy.error.scopeNotAllowed',
    ];
    expect(keys.length).toBeGreaterThan(60);
    for (const key of mapped) expect(en[key], key).toBeTruthy();
  });

  it.each(files.filter((f) => f !== 'en.json'))(
    '%s is translated, keeps every placeholder and never has an empty string',
    (file) => {
      const locale = load(file);
      const sameAsEnglish = keys.filter((k) => locale[k] === en[k]);
      expect(sameAsEnglish).toEqual([]);
      const broken = keys.filter(
        (k) => !locale[k]?.trim() || placeholders(locale[k]).join() !== placeholders(en[k]).join()
      );
      expect(broken).toEqual([]);
    }
  );
});
