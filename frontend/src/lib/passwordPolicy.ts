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
  /** Whether a breached-password list is installed; the check is skipped without one. */
  blocklist_status?: {
    installed: boolean;
    entries: number;
    source: 'default' | 'custom';
    retrieved: string | null;
  };
  online_check_enabled?: boolean;
  max_length?: number;
  min_length_with_mfa?: number;
  history_count?: number;
  max_age_days?: number;
  min_age_hours?: number;
}

export interface PasswordRequirement {
  key: string;
  params?: Record<string, number>;
}

/** What the hardened (DoD-style) tier enforces; shown if the policy cannot be fetched. */
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

/** The policy as the server enforces it right now, bypassing the once-per-page cache. */
export async function fetchPasswordPolicy(): Promise<PasswordPolicy> {
  const { data } = await axiosInstance.get<PasswordPolicy>('/auth/password-policy');
  cached = Promise.resolve(data);
  return data;
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
  // Without an installed list the check is skipped, so do not promise it.
  if (policy.blocklist_enabled && policy.blocklist_status?.installed !== false)
    items.push({ key: 'auth.passwordReqNotBreached' });
  return items;
}
