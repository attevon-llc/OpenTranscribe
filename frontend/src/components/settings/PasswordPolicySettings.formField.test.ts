import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key) => key);
      return () => {};
    },
  },
}));
vi.mock('$lib/passwordPolicy', () => ({
  fetchPasswordPolicy: vi.fn().mockRejectedValue(new Error('offline')),
}));

import PasswordPolicySettings from './PasswordPolicySettings.svelte';

const data = {
  password_policy_profile: 'custom',
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
};

describe('PasswordPolicySettings FormField wiring', () => {
  it('keeps the input ids and points hinted inputs at their hint', () => {
    const { container } = render(PasswordPolicySettings, { data });
    const maxAge = container.querySelector('#password_max_age_days') as HTMLInputElement;
    expect(maxAge).not.toBeNull();
    expect(container.querySelector('label[for="password_max_age_days"]')).not.toBeNull();
    expect(maxAge.getAttribute('aria-describedby')).toBe('password_max_age_days-hint');
    expect(container.querySelector('#password_max_age_days-hint')?.textContent).toBe(
      'settings.localAuth.passwordExpiryHelp'
    );
    // no hint, so no dangling reference
    expect(container.querySelector('#password_min_length')?.hasAttribute('aria-describedby')).toBe(
      false
    );
  });
});
