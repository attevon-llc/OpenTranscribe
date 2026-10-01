/**
 * The password-policy tiers an admin can pick, and the rules each one enforces.
 *
 * Mirrors backend/app/auth/password_policy.py (`is_preset`, `min_length_for`, ...). The
 * server is the authority; this only previews what a selection will do before it is
 * saved, so keep the two in step.
 */

export const PASSWORD_TIERS = ['basic', 'standard', 'hardened', 'custom'] as const;
export type PasswordTier = (typeof PASSWORD_TIERS)[number];

/** Tiers whose rules are fixed; the individual values below are then ignored. */
export const PRESET_TIERS: readonly PasswordTier[] = ['basic', 'standard'];

/** The single-factor / MFA minimums of the presets (NIST SP 800-63B-4 for `standard`). */
const BASIC_MIN = 8;
const STANDARD_MIN = 15;
const STANDARD_MIN_WITH_MFA = 8;
const PRESET_DEFAULT_MAX = 128;
const PRESET_MIN_ACCEPTED_MAX = 64;

/** `nist` and `stig` are the earlier spellings of `standard` and `hardened`. */
export function normalizeTier(value: unknown): PasswordTier {
  const name = String(value ?? '')
    .trim()
    .toLowerCase();
  if (name === 'nist') return 'standard';
  if (name === 'stig') return 'hardened';
  return (PASSWORD_TIERS as readonly string[]).includes(name) ? (name as PasswordTier) : 'hardened';
}

export function isPreset(tier: PasswordTier): boolean {
  return PRESET_TIERS.includes(tier);
}

/** The individual values stored for the policy (what `custom` and `hardened` use). */
export interface PolicyValues {
  password_min_length: number;
  password_max_length: number;
  password_require_uppercase: boolean;
  password_require_lowercase: boolean;
  password_require_digit: boolean;
  password_require_special: boolean;
  password_history_count: number;
  password_max_age_days: number;
  password_min_age_hours: number;
  /** '' = the tier's default, otherwise 'true' / 'false'. */
  password_blocklist_enabled: string;
}

export interface EffectiveRules {
  minLength: number;
  /** Minimum for an account protected by MFA; equals `minLength` unless the tier differs. */
  minLengthWithMfa: number;
  /** 0 = no maximum. */
  maxLength: number;
  uppercase: boolean;
  lowercase: boolean;
  digit: boolean;
  special: boolean;
  historyCount: number;
  maxAgeDays: number;
  minAgeHours: number;
  blocklist: boolean;
}

export function effectiveRules(tier: PasswordTier, values: PolicyValues): EffectiveRules {
  const preset = isPreset(tier);
  const override = String(values.password_blocklist_enabled ?? '')
    .trim()
    .toLowerCase();
  const configuredMax = Number(values.password_max_length) || 0;
  const minLength =
    tier === 'basic' ? BASIC_MIN : tier === 'standard' ? STANDARD_MIN : values.password_min_length;
  return {
    minLength,
    minLengthWithMfa: tier === 'standard' ? STANDARD_MIN_WITH_MFA : minLength,
    maxLength: preset
      ? configuredMax > 0
        ? Math.max(configuredMax, PRESET_MIN_ACCEPTED_MAX)
        : PRESET_DEFAULT_MAX
      : configuredMax,
    uppercase: !preset && values.password_require_uppercase,
    lowercase: !preset && values.password_require_lowercase,
    digit: !preset && values.password_require_digit,
    special: !preset && values.password_require_special,
    historyCount: preset ? 0 : values.password_history_count,
    maxAgeDays: preset ? 0 : values.password_max_age_days,
    minAgeHours: preset ? 0 : values.password_min_age_hours,
    blocklist: override ? ['true', '1', 'yes', 'on'].includes(override) : preset,
  };
}
