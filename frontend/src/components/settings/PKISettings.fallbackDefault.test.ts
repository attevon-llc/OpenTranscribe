/**
 * `pki_allow_password_fallback` is a deployment-level CEILING: effective
 * fallback is `user.allow_local_fallback AND pki_allow_password_fallback`. Its
 * backend default is `true` (`PKIConfig` in backend/app/schemas/auth_config.py)
 * precisely because a `true` ceiling restricts nothing, while a `false` one
 * revokes fallback from every account a super_admin granted it to.
 *
 * The panel defaulted the field to `false`, so an admin opening the PKI tab on
 * an unconfigured deployment and saving ANYTHING wrote `false` and locked those
 * users out. The assertions are on the dispatched payload — that write is what
 * caused the lockout.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import PKISettings from './PKISettings.svelte';

function renderPanel(props: Record<string, unknown> = { config: {} }, onSave = vi.fn()) {
  render(PKISettings, { props, events: { save: onSave } } as never);
  return onSave;
}

async function saveAndCapture(props: Record<string, unknown>) {
  const onSave = renderPanel(props);
  await fireEvent.click(screen.getByRole('button', { name: 'settings.pki.saveConfiguration' }));
  expect(onSave).toHaveBeenCalledTimes(1);
  return (onSave.mock.calls[0][0] as CustomEvent).detail as Record<string, unknown>;
}

function fallbackCheckbox(): HTMLInputElement {
  return screen.getByLabelText('settings.pki.allowPasswordFallback') as HTMLInputElement;
}

describe('PKISettings — password fallback ceiling', () => {
  it('never writes a false ceiling for an unconfigured deployment', async () => {
    const payload = await saveAndCapture({ config: {} });

    expect(payload.pki_allow_password_fallback).toBe(true);
  });

  it('matches the backend default in the rendered form', () => {
    renderPanel();

    expect(fallbackCheckbox()).toBeChecked();
  });

  it('respects a ceiling an admin deliberately cleared', async () => {
    const payload = await saveAndCapture({
      config: { pki_enabled: true, pki_allow_password_fallback: false },
    });

    expect(payload.pki_allow_password_fallback).toBe(false);
  });

  it('keeps an enabled ceiling enabled', async () => {
    const payload = await saveAndCapture({
      config: { pki_enabled: true, pki_allow_password_fallback: true },
    });

    expect(payload.pki_allow_password_fallback).toBe(true);
  });

  it('explains that the flag is a ceiling over the per-user setting', () => {
    renderPanel();

    expect(screen.getByText('settings.pki.allowPasswordFallbackHelp')).toBeInTheDocument();
  });

  it('warns only while the ceiling is cleared', async () => {
    renderPanel({ config: { pki_enabled: true } });

    expect(screen.queryByText('settings.pki.allowPasswordFallbackWarning')).not.toBeInTheDocument();

    await fireEvent.click(fallbackCheckbox());

    expect(screen.getByText('settings.pki.allowPasswordFallbackWarning')).toBeInTheDocument();
  });
});
