/**
 * `concurrent_session_policy` is `Literal["terminate_oldest", "reject"]` on the
 * backend (`SessionConfig` in backend/app/schemas/auth_config.py). This panel
 * used to offer `oldest` / `newest` / `all`, so NO value it could produce ever
 * matched and the concurrent-session limit enforced nothing whichever radio the
 * admin picked.
 *
 * The assertions are on the dispatched PAYLOAD, because the payload is what got
 * written to the database and silently did nothing.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  // Identity translator so queries can match on the i18n key.
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import SessionSettings from './SessionSettings.svelte';

/** The two literals `auth_settings.concurrent_session_policy` compares against. */
const BACKEND_POLICIES = ['terminate_oldest', 'reject'];

function renderPanel(props: Record<string, unknown> = { config: {} }, onSave = vi.fn()) {
  render(SessionSettings, { props, events: { save: onSave } } as never);
  return onSave;
}

async function saveAndCapture(props: Record<string, unknown>) {
  const onSave = renderPanel(props);
  await fireEvent.click(screen.getByRole('button', { name: 'settings.session.saveConfiguration' }));
  expect(onSave).toHaveBeenCalledTimes(1);
  return (onSave.mock.calls[0][0] as CustomEvent).detail as Record<string, unknown>;
}

function policyRadios(): HTMLInputElement[] {
  return screen.getAllByRole('radio') as HTMLInputElement[];
}

describe('SessionSettings — concurrent session policy', () => {
  it('submits a policy the backend can match on an unconfigured deployment', async () => {
    const payload = await saveAndCapture({ config: {} });

    expect(payload.concurrent_session_policy).toBe('terminate_oldest');
    expect(BACKEND_POLICIES).toContain(payload.concurrent_session_policy);
  });

  it('offers only values the backend understands', () => {
    renderPanel();

    expect(policyRadios().map((input) => input.value)).toEqual(BACKEND_POLICIES);
  });

  it('drops the `all` option, which had no backend meaning at all', () => {
    renderPanel();

    const values = policyRadios().map((input) => input.value);
    expect(values).not.toContain('all');
    expect(values).not.toContain('oldest');
    expect(values).not.toContain('newest');
  });

  it('submits `reject` when the admin picks refusing the new sign-in', async () => {
    const onSave = renderPanel();

    await fireEvent.click(
      policyRadios().find((input) => input.value === 'reject') as HTMLInputElement
    );
    await fireEvent.click(
      screen.getByRole('button', { name: 'settings.session.saveConfiguration' })
    );

    expect((onSave.mock.calls[0][0] as CustomEvent).detail.concurrent_session_policy).toBe(
      'reject'
    );
  });

  it('preserves a stored value rather than resetting it to the default', async () => {
    const payload = await saveAndCapture({ config: { concurrent_session_policy: 'reject' } });

    expect(payload.concurrent_session_policy).toBe('reject');
  });

  it('labels the options for humans', () => {
    renderPanel();

    expect(screen.getByText('settings.session.policyOldestLabel')).toBeInTheDocument();
    expect(screen.getByText('settings.session.policyRejectLabel')).toBeInTheDocument();
  });
});

describe('SessionSettings — restart-required token lifetimes', () => {
  it('badges both JWT lifetimes rather than implying they apply immediately', () => {
    renderPanel();

    // `AuthConfigService.RESTART_REQUIRED_KEYS` holds exactly these two keys —
    // app/auth/cookies.py sizes the session cookies from them at import time.
    expect(screen.getAllByText('settings.session.restartRequired')).toHaveLength(2);
    expect(screen.getByText('settings.session.restartRequiredHelp')).toBeInTheDocument();
  });

  it('does not badge the settings that really are live', () => {
    renderPanel();

    const badged = screen
      .getAllByText('settings.session.restartRequired')
      .map((badge) => badge.parentElement?.getAttribute('for'));

    expect(badged).toEqual(['jwt_access_token_expire_minutes', 'jwt_refresh_token_expire_days']);
  });
});
