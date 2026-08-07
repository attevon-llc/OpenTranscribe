import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/api/authConfig', () => ({
  AuthConfigApi: {
    getAllConfigs: vi.fn(),
    updateCategory: vi.fn(),
    testConnection: vi.fn(),
  },
}));

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  // Identity translator so queries can match on the i18n key.
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { AuthConfigApi } from '$lib/api/authConfig';
import { toastStore } from '$stores/toast';
import AuthenticationSettings from './AuthenticationSettings.svelte';

const mockedGetAll = vi.mocked(AuthConfigApi.getAllConfigs);
const mockedUpdate = vi.mocked(AuthConfigApi.updateCategory);

/** Build the wire shape the endpoint returns: category -> array of config rows. */
function row(config_key: string, config_value: unknown, data_type: string) {
  return { config_key, config_value: String(config_value), data_type };
}

/**
 * Stored values that all differ from the panel's coded defaults, so a value only
 * appears in the form if it was genuinely loaded from its own category.
 */
function storedConfigs() {
  return {
    local: [
      row('local_enabled', true, 'bool'),
      row('allow_registration', false, 'bool'),
      // Legacy alias rows that still linger under `local`. Nothing reads them and
      // the panel must not resurrect them.
      row('password_require_numbers', false, 'bool'),
      row('mfa_issuer', 'StaleIssuer', 'string'),
      row('max_login_attempts', 9, 'int'),
      row('lockout_duration_minutes', 99, 'int'),
    ],
    password_policy: [
      row('password_min_length', 16, 'int'),
      row('password_require_uppercase', true, 'bool'),
      row('password_require_lowercase', true, 'bool'),
      row('password_require_digit', true, 'bool'),
      row('password_require_special', false, 'bool'),
      row('password_max_age_days', 45, 'int'),
      row('password_history_count', 12, 'int'),
    ],
    mfa: [
      row('mfa_enabled', true, 'bool'),
      row('mfa_required', true, 'bool'),
      row('mfa_issuer_name', 'AcmeCorp', 'string'),
    ],
    lockout: [
      row('account_lockout_threshold', 7, 'int'),
      row('account_lockout_duration_minutes', 20, 'int'),
    ],
  };
}

async function renderLocalTab() {
  render(AuthenticationSettings);
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: 'settings.localAuth.saveConfiguration' })
    ).toBeInTheDocument()
  );
}

async function save() {
  await fireEvent.click(
    screen.getByRole('button', { name: 'settings.localAuth.saveConfiguration' })
  );
  await waitFor(() => expect(mockedUpdate).toHaveBeenCalled());
}

/** The payload PUT to a given category, or undefined if it was never called. */
function payloadFor(category: string) {
  return mockedUpdate.mock.calls.find((call) => call[0] === category)?.[1] as
    | Record<string, unknown>
    | undefined;
}

describe('Local auth tab — split save', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetAll.mockResolvedValue(storedConfigs() as never);
    mockedUpdate.mockResolvedValue(undefined as never);
  });

  it('loads values from all four source categories, not just `local`', async () => {
    await renderLocalTab();

    // From password_policy
    expect(screen.getByLabelText('settings.localAuth.minPasswordLength')).toHaveValue(16);
    expect(screen.getByLabelText('settings.localAuth.passwordExpiry')).toHaveValue(45);
    // From mfa
    expect(screen.getByLabelText('settings.localAuth.mfaIssuerName')).toHaveValue('AcmeCorp');
    // From lockout
    expect(screen.getByLabelText('settings.localAuth.maxLoginAttempts')).toHaveValue(7);
    expect(screen.getByLabelText('settings.localAuth.lockoutDuration')).toHaveValue(20);
  });

  it('fans the single form out to its four owning categories', async () => {
    await renderLocalTab();
    await save();

    expect(mockedUpdate).toHaveBeenCalledTimes(4);
    expect(new Set(mockedUpdate.mock.calls.map((call) => call[0]))).toEqual(
      new Set(['local', 'password_policy', 'mfa', 'lockout'])
    );

    // Each category receives exactly its own keys — the endpoint 400s on strays.
    // `require_email_verification` / `require_account_approval` belong to `local`
    // too — they are the other half of "who may get an account here".
    expect(Object.keys(payloadFor('local') ?? {}).sort()).toEqual([
      'allow_registration',
      'local_enabled',
      'require_account_approval',
      'require_email_verification',
    ]);
    expect(Object.keys(payloadFor('mfa') ?? {}).sort()).toEqual([
      'mfa_enabled',
      'mfa_issuer_name',
      'mfa_required',
    ]);
    expect(Object.keys(payloadFor('lockout') ?? {}).sort()).toEqual([
      'account_lockout_duration_minutes',
      'account_lockout_threshold',
    ]);
    expect(Object.keys(payloadFor('password_policy') ?? {})).toHaveLength(7);
    expect(payloadFor('password_policy')).toMatchObject({
      password_min_length: 16,
      password_require_digit: true,
      password_history_count: 12,
    });
  });

  it('never writes the dead legacy aliases back', async () => {
    await renderLocalTab();
    await save();

    const everyKey = mockedUpdate.mock.calls.flatMap((call) =>
      Object.keys((call[1] ?? {}) as Record<string, unknown>)
    );
    expect(everyKey).not.toContain('password_require_numbers');
    expect(everyKey).not.toContain('mfa_issuer');
    expect(everyKey).not.toContain('max_login_attempts');
    expect(everyKey).not.toContain('lockout_duration_minutes');
  });

  it('reports which category failed instead of claiming success', async () => {
    mockedUpdate.mockImplementation((category: string) =>
      category === 'mfa'
        ? (Promise.reject(new Error('400')) as never)
        : (Promise.resolve(undefined) as never)
    );

    await renderLocalTab();
    await save();

    await waitFor(() => expect(toastStore.error).toHaveBeenCalled());
    expect(toastStore.success).not.toHaveBeenCalled();
    expect(vi.mocked(toastStore.error).mock.calls[0][0]).toBe(
      'settings.authentication.configSavePartialFailure'
    );
  });

  it('reports a clean save once every category lands', async () => {
    await renderLocalTab();
    await save();

    await waitFor(() => expect(toastStore.success).toHaveBeenCalled());
    expect(vi.mocked(toastStore.success).mock.calls[0][0]).toBe(
      'settings.authentication.localConfigSaved'
    );
    expect(toastStore.error).not.toHaveBeenCalled();
  });
});
