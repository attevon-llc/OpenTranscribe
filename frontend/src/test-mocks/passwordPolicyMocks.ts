/**
 * Factory for the `$lib/passwordPolicy` module mock: the real module with `loadPasswordPolicy`
 * resolving the fallback policy, so the page under test never requests `/auth/password-policy`.
 * `vi.mock('$lib/passwordPolicy', async () =>
 * (await import('$testMocks/passwordPolicyMocks')).passwordPolicyMock())`.
 */
import { vi } from 'vitest';

export async function passwordPolicyMock() {
  const actual = await vi.importActual<typeof import('$lib/passwordPolicy')>('$lib/passwordPolicy');
  return {
    ...actual,
    loadPasswordPolicy: vi.fn().mockResolvedValue(actual.FALLBACK_PASSWORD_POLICY),
  };
}
