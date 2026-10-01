import axiosInstance from '$lib/axios';

/** The subset of GET /auth/password-policy the requirement lists render. */
export interface PasswordPolicy {
  profile?: string;
  min_length: number;
  require_uppercase: boolean;
  require_lowercase: boolean;
  require_digit: boolean;
  require_special: boolean;
  blocklist_enabled?: boolean;
}

export interface PasswordRequirement {
  key: string;
  params?: Record<string, number>;
}

/** What the DoD-style profile enforces; shown if the policy cannot be fetched. */
export const FALLBACK_PASSWORD_POLICY: PasswordPolicy = {
  min_length: 12,
  require_uppercase: true,
  require_lowercase: true,
  require_digit: true,
  require_special: true,
  blocklist_enabled: false,
};

let cached: Promise<PasswordPolicy> | null = null;

/** Server-owned policy, fetched once per page load. */
export function loadPasswordPolicy(): Promise<PasswordPolicy> {
  if (!cached) {
    cached = axiosInstance
      .get<PasswordPolicy>('/auth/password-policy')
      .then(({ data }) => data)
      .catch(() => {
        cached = null;
        return FALLBACK_PASSWORD_POLICY;
      });
  }
  return cached;
}

/** i18n keys (and params) for the requirement bullets the active policy enforces. */
export function buildPasswordRequirements(policy: PasswordPolicy): PasswordRequirement[] {
  const items: PasswordRequirement[] = [
    { key: 'auth.passwordReqMinLength', params: { min: policy.min_length } },
  ];
  if (policy.require_uppercase) items.push({ key: 'auth.passwordReqUppercase' });
  if (policy.require_lowercase) items.push({ key: 'auth.passwordReqLowercase' });
  if (policy.require_digit) items.push({ key: 'auth.passwordReqNumber' });
  if (policy.require_special) items.push({ key: 'auth.passwordReqSpecial' });
  const composition =
    policy.require_uppercase ||
    policy.require_lowercase ||
    policy.require_digit ||
    policy.require_special;
  if (!composition) items.push({ key: 'auth.passwordReqAnyCharacters' });
  if (policy.blocklist_enabled) items.push({ key: 'auth.passwordReqNotBreached' });
  return items;
}
