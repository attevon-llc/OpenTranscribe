import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn() },
  abortAllRequests: vi.fn(),
}));

vi.mock('$stores/locale', () => ({
  // Identity translator — assertions key off the i18n key, which also proves the
  // message is translated rather than a hardcoded English string.
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$lib/session/clearUserState', () => ({ clearUserState: vi.fn() }));
vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

import axiosInstance from '$lib/axios';
import { login, setupMfaEnrollment, verifyMfaEnrollment, authStore } from './auth';

const mockedGet = vi.mocked(axiosInstance.get);
const mockedPost = vi.mocked(axiosInstance.post);

const ENROLL_TOKEN = 'half-token-abc';

function rejectWith(status: number, detail: string) {
  return Promise.reject({ response: { status, data: { detail } } });
}

describe('login MFA branching', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('flags forced enrolment when the server sets mfa_enrollment_required', async () => {
    mockedPost.mockResolvedValueOnce({
      status: 200,
      data: {
        mfa_required: true,
        mfa_enrollment_required: true,
        mfa_token: ENROLL_TOKEN,
        message: 'MFA enrollment is required before access is granted',
      },
    } as never);

    const result = await login('someone@example.com', 'correct-horse');

    expect(result.success).toBe(false);
    expect(result.mfa_required).toBe(true);
    expect(result.mfa_enrollment_required).toBe(true);
    expect(result.mfa_token).toBe(ENROLL_TOKEN);
  });

  it('treats an absent mfa_enrollment_required as a plain TOTP challenge', async () => {
    mockedPost.mockResolvedValueOnce({
      status: 200,
      data: { mfa_required: true, mfa_token: 'verify-token', message: 'MFA verification required' },
    } as never);

    const result = await login('someone@example.com', 'correct-horse');

    expect(result.mfa_required).toBe(true);
    // Strictly false, never undefined — the login page branches on it.
    expect(result.mfa_enrollment_required).toBe(false);
  });
});

describe('setupMfaEnrollment', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('authenticates with the half-token as a bearer, since no cookie exists yet', async () => {
    mockedPost.mockResolvedValueOnce({
      data: { secret: 'S3CR3T', provisioning_uri: 'otpauth://x', qr_code_base64: 'AAA' },
    } as never);

    const result = await setupMfaEnrollment(ENROLL_TOKEN);

    expect(result.success).toBe(true);
    expect(mockedPost).toHaveBeenCalledWith('/auth/mfa/setup', undefined, {
      headers: { Authorization: `Bearer ${ENROLL_TOKEN}` },
    });
  });

  it('omits the bearer header when driven by an existing cookie session', async () => {
    mockedPost.mockResolvedValueOnce({
      data: { secret: 'S3CR3T', provisioning_uri: 'otpauth://x', qr_code_base64: 'AAA' },
    } as never);

    await setupMfaEnrollment();

    expect(mockedPost).toHaveBeenCalledWith('/auth/mfa/setup', undefined, {});
  });

  it('classifies an external-IdP account as unavailable, not retryable', async () => {
    mockedPost.mockReturnValueOnce(
      rejectWith(400, 'MFA setup is not available for your authentication type') as never
    );

    const result = await setupMfaEnrollment(ENROLL_TOKEN);

    expect(result.success).toBe(false);
    if (result.success) return;
    expect(result.error.kind).toBe('unavailable');
    expect(result.error.message).toBe('auth.mfaEnroll.error.unavailable');
  });
});

describe('verifyMfaEnrollment', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authStore.reset();
  });

  it('returns the backup codes and hydrates the session it was just handed', async () => {
    mockedPost.mockResolvedValueOnce({
      data: { success: true, backup_codes: ['AAAA-1111', 'BBBB-2222'], access_token: 'real' },
    } as never);
    mockedGet.mockResolvedValueOnce({
      data: { uuid: 'u1', email: 'someone@example.com', role: 'user' },
    } as never);

    const result = await verifyMfaEnrollment('123456', ENROLL_TOKEN);

    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.backupCodes).toEqual(['AAAA-1111', 'BBBB-2222']);
    expect(mockedPost).toHaveBeenCalledWith(
      '/auth/mfa/verify-setup',
      { code: '123456' },
      { headers: { Authorization: `Bearer ${ENROLL_TOKEN}` } }
    );
    // Cookies came back with the response, so the user is already logged in.
    const state = get(authStore);
    expect(state.isAuthenticated).toBe(true);
    expect(state.user?.email).toBe('someone@example.com');
  });

  it('keeps a mistyped code retryable — the half-token survives it', async () => {
    mockedPost.mockReturnValueOnce(
      rejectWith(400, 'Invalid verification code. Please try again.') as never
    );

    const result = await verifyMfaEnrollment('000000', ENROLL_TOKEN);

    expect(result.success).toBe(false);
    if (result.success) return;
    expect(result.error.kind).toBe('retry');
    expect(result.error.message).toBe('auth.mfaEnroll.error.invalidCode');
  });

  it('sends a spent or expired half-token back to the login form', async () => {
    mockedPost.mockReturnValueOnce(rejectWith(401, 'MFA token has already been used') as never);

    const result = await verifyMfaEnrollment('123456', ENROLL_TOKEN);

    expect(result.success).toBe(false);
    if (result.success) return;
    expect(result.error.kind).toBe('expired');
  });

  it('restarts setup when the server says MFA is already enabled', async () => {
    mockedPost.mockReturnValueOnce(rejectWith(400, 'MFA is already enabled.') as never);

    const result = await verifyMfaEnrollment('123456', ENROLL_TOKEN);

    expect(result.success).toBe(false);
    if (result.success) return;
    expect(result.error.kind).toBe('restart');
  });
});
