import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  // Identity translator with the params appended, so queries match on key and values.
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => {
      run((key, params) => (params ? `${key} ${JSON.stringify(params)}` : key));
      return () => {};
    },
  },
}));

vi.mock('$lib/passwordPolicy', () => ({ fetchPasswordPolicy: vi.fn() }));

import { fetchPasswordPolicy } from '$lib/passwordPolicy';
import PasswordPolicySettings from './PasswordPolicySettings.svelte';

const mockedFetch = vi.mocked(fetchPasswordPolicy);

function formData(overrides: Record<string, unknown> = {}) {
  return {
    password_policy_profile: 'hardened',
    password_min_length: 12,
    password_max_length: 0,
    password_require_uppercase: true,
    password_require_lowercase: true,
    password_require_digit: true,
    password_require_special: true,
    password_history_count: 24,
    password_max_age_days: 60,
    password_min_age_hours: 24,
    password_blocklist_enabled: '',
    password_hibp_enabled: false,
    ...overrides,
  };
}

const rules = () => screen.getByTestId('password-tier-rules').textContent ?? '';

describe('PasswordPolicySettings tier picker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetch.mockRejectedValue(new Error('offline'));
  });

  it('offers exactly the four tiers and selects the stored one', () => {
    render(PasswordPolicySettings, { data: formData({ password_policy_profile: 'standard' }) });
    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((r) => r.value)).toEqual(['basic', 'standard', 'hardened', 'custom']);
    expect(radios.find((r) => r.checked)?.value).toBe('standard');
  });

  it.each([
    ['nist', 'standard'],
    ['stig', 'hardened'],
  ])('shows the old name %s as the %s tier', (stored, tier) => {
    render(PasswordPolicySettings, { data: formData({ password_policy_profile: stored }) });
    const checked = (screen.getAllByRole('radio') as HTMLInputElement[]).find((r) => r.checked);
    expect(checked?.value).toBe(tier);
  });

  it('switching to Basic previews 8 characters, no composition, no expiry', async () => {
    render(PasswordPolicySettings, { data: formData() });
    expect(rules()).toContain('"min":12');
    expect(rules()).toContain('rule.composition');

    await fireEvent.click(screen.getByRole('radio', { name: /basic\.name/ }));

    expect(rules()).toContain('"min":8');
    expect(rules()).toContain('rule.noComposition');
    expect(rules()).toContain('rule.noExpiry');
    expect(rules()).toContain('rule.noHistory');
  });

  it('Standard previews 15 characters (8 with MFA) and hides the individual values', async () => {
    render(PasswordPolicySettings, { data: formData() });
    await fireEvent.click(screen.getByRole('radio', { name: /standard\.name/ }));
    expect(rules()).toContain('"single":15');
    expect(rules()).toContain('"withMfa":8');
    expect(rules()).toContain('rule.blocklistOn');
    expect(screen.queryByLabelText('settings.localAuth.passwordExpiry')).toBeNull();
    expect(screen.queryByLabelText('settings.localAuth.minPasswordLength')).toBeNull();
  });

  it('Hardened shows the stored values read-only', () => {
    render(PasswordPolicySettings, { data: formData() });
    expect(screen.getByLabelText('settings.localAuth.minPasswordLength')).toBeDisabled();
    expect(screen.getByLabelText('settings.localAuth.passwordExpiry')).toBeDisabled();
  });

  it('Custom makes every value editable and previews the edit', async () => {
    render(PasswordPolicySettings, { data: formData({ password_policy_profile: 'custom' }) });
    const minLength = screen.getByLabelText('settings.localAuth.minPasswordLength');
    expect(minLength).toBeEnabled();
    expect(screen.getByLabelText('settings.localAuth.passwordExpiry')).toBeEnabled();

    await fireEvent.input(minLength, { target: { value: '9' } });
    expect(rules()).toContain('"min":9');
  });

  it('tells the admin when the maximum length is below the minimum', async () => {
    render(PasswordPolicySettings, { data: formData({ password_policy_profile: 'custom' }) });
    await fireEvent.input(screen.getByLabelText('settings.passwordTier.maxLength'), {
      target: { value: '10' },
    });
    expect(screen.getByRole('alert')).toHaveTextContent('settings.passwordTier.lengthConflict');
  });

  it('is inert while local login is off', () => {
    render(PasswordPolicySettings, { data: formData(), disabled: true });
    for (const radio of screen.getAllByRole('radio')) expect(radio).toBeDisabled();
  });
});

describe('PasswordPolicySettings breached-password list status', () => {
  const policy = (status: Record<string, unknown>) =>
    ({ min_length: 12, blocklist_status: status }) as never;

  it('says so, with the install command, when no list is installed', async () => {
    mockedFetch.mockResolvedValue(
      policy({ installed: false, entries: 0, source: 'default', retrieved: null })
    );
    render(PasswordPolicySettings, { data: formData() });
    const status = await screen.findByTestId('blocklist-status');
    expect(status).toHaveTextContent('settings.passwordTier.listMissing');
    expect(status).toHaveTextContent('./opentranscribe.sh download-models password-blocklist');
  });

  it('does not promise the check in the rules preview while the list is missing', async () => {
    mockedFetch.mockResolvedValue(
      policy({ installed: false, entries: 0, source: 'default', retrieved: null })
    );
    render(PasswordPolicySettings, { data: formData({ password_policy_profile: 'standard' }) });
    await screen.findByTestId('blocklist-status');
    await waitFor(() => expect(rules()).toContain('rule.blocklistOff'));
    expect(rules()).not.toContain('rule.blocklistOn');
  });

  it('shows the size and date of an installed list', async () => {
    mockedFetch.mockResolvedValue(
      policy({ installed: true, entries: 100000, source: 'default', retrieved: '2026-10-01' })
    );
    render(PasswordPolicySettings, { data: formData() });
    const status = await screen.findByTestId('blocklist-status');
    await waitFor(() => expect(status).toHaveTextContent('listInstalledDated'));
    expect(status).toHaveTextContent('2026-10-01');
    expect(status).not.toHaveTextContent('download-models');
  });

  it('shows nothing when the status is unknown', async () => {
    mockedFetch.mockRejectedValue(new Error('offline'));
    render(PasswordPolicySettings, { data: formData() });
    await waitFor(() => expect(mockedFetch).toHaveBeenCalled());
    expect(screen.queryByTestId('blocklist-status')).toBeNull();
  });
});
