import { describe, it, expect } from 'vitest';
import { effectiveRules, isPreset, normalizeTier, type PolicyValues } from './passwordPolicyTiers';

const strictValues: PolicyValues = {
  password_min_length: 14,
  password_max_length: 0,
  password_require_uppercase: true,
  password_require_lowercase: true,
  password_require_digit: true,
  password_require_special: true,
  password_history_count: 24,
  password_max_age_days: 60,
  password_min_age_hours: 24,
  password_blocklist_enabled: '',
};

describe('normalizeTier', () => {
  it('keeps the four tiers', () => {
    for (const tier of ['basic', 'standard', 'hardened', 'custom']) {
      expect(normalizeTier(tier)).toBe(tier);
    }
  });

  it('maps the earlier names onto their tiers', () => {
    expect(normalizeTier('nist')).toBe('standard');
    expect(normalizeTier(' STIG ')).toBe('hardened');
  });

  it('falls back to hardened, the strictest, for anything else', () => {
    expect(normalizeTier(undefined)).toBe('hardened');
    expect(normalizeTier('paranoid')).toBe('hardened');
  });
});

describe('effectiveRules', () => {
  it('basic: 8 characters, nothing else, whatever the stored values say', () => {
    const rules = effectiveRules('basic', strictValues);
    expect(rules).toMatchObject({
      minLength: 8,
      minLengthWithMfa: 8,
      uppercase: false,
      lowercase: false,
      digit: false,
      special: false,
      historyCount: 0,
      maxAgeDays: 0,
      minAgeHours: 0,
      maxLength: 128,
      blocklist: true,
    });
  });

  it('standard: 15 characters, 8 with MFA, no rotation', () => {
    const rules = effectiveRules('standard', strictValues);
    expect(rules).toMatchObject({
      minLength: 15,
      minLengthWithMfa: 8,
      digit: false,
      historyCount: 0,
      maxAgeDays: 0,
      blocklist: true,
    });
  });

  it('hardened and custom use the stored values; the blocklist is off unless switched on', () => {
    for (const tier of ['hardened', 'custom'] as const) {
      const rules = effectiveRules(tier, strictValues);
      expect(rules).toMatchObject({
        minLength: 14,
        uppercase: true,
        special: true,
        historyCount: 24,
        maxAgeDays: 60,
        minAgeHours: 24,
        maxLength: 0,
        blocklist: false,
      });
      expect(
        effectiveRules(tier, { ...strictValues, password_blocklist_enabled: 'true' }).blocklist
      ).toBe(true);
    }
  });

  it('an explicit override beats the tier default', () => {
    expect(
      effectiveRules('standard', { ...strictValues, password_blocklist_enabled: 'false' }).blocklist
    ).toBe(false);
  });

  it('presets never accept a maximum below 64', () => {
    expect(effectiveRules('standard', { ...strictValues, password_max_length: 20 }).maxLength).toBe(
      64
    );
    expect(
      effectiveRules('standard', { ...strictValues, password_max_length: 200 }).maxLength
    ).toBe(200);
  });

  it('knows which tiers are presets', () => {
    expect(['basic', 'standard'].every((t) => isPreset(t as never))).toBe(true);
    expect(['hardened', 'custom'].some((t) => isPreset(t as never))).toBe(false);
  });
});
