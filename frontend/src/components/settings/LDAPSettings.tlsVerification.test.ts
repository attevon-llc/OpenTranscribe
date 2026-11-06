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

import LDAPSettings from './LDAPSettings.svelte';

function renderPanel(config: Record<string, unknown>, onSave = vi.fn(), onTest = vi.fn()) {
  render(LDAPSettings, {
    props: { config: { ldap_enabled: true, ldap_server: 'ldap.example.com', ...config } },
    events: { save: onSave, test: onTest },
  } as never);
  return { onSave, onTest };
}

function payload(handler: ReturnType<typeof vi.fn>): Record<string, unknown> {
  expect(handler).toHaveBeenCalledTimes(1);
  return (handler.mock.calls[0][0] as CustomEvent).detail as Record<string, unknown>;
}

describe('LDAP panel: directory certificate verification', () => {
  it('shows the stored CA file and keeps verification on by default', () => {
    renderPanel({ ldap_ca_cert_file: '/etc/ssl/directory-ca.pem' });

    const file = screen.getByLabelText('settings.ldap.caCertFile') as HTMLInputElement;
    const skip = screen.getByLabelText('settings.ldap.tlsSkipVerify') as HTMLInputElement;
    expect(file.value).toBe('/etc/ssl/directory-ca.pem');
    expect(skip.checked).toBe(false);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('sends the verification settings on save and on the connection test', async () => {
    const { onSave, onTest } = renderPanel({
      ldap_ca_cert_file: '/ca.pem',
      ldap_tls_server_name: 'directory.example',
    });
    const skip = screen.getByLabelText('settings.ldap.tlsSkipVerify');

    await fireEvent.click(skip);
    await fireEvent.click(screen.getByRole('button', { name: 'settings.ldap.saveConfiguration' }));
    await fireEvent.click(screen.getByRole('button', { name: 'settings.ldap.testConnection' }));

    for (const sent of [payload(onSave), payload(onTest)]) {
      expect(sent.ldap_ca_cert_file).toBe('/ca.pem');
      expect(sent.ldap_tls_server_name).toBe('directory.example');
      expect(sent.ldap_tls_skip_verify).toBe(true);
    }
  });

  it('shows the stored server name and lets it be edited', async () => {
    const { onSave } = renderPanel({ ldap_tls_server_name: 'directory.example' });
    const name = screen.getByLabelText('settings.ldap.tlsServerName') as HTMLInputElement;
    expect(name.value).toBe('directory.example');

    await fireEvent.input(name, { target: { value: 'ldap.corp.example' } });
    await fireEvent.click(screen.getByRole('button', { name: 'settings.ldap.saveConfiguration' }));

    expect(payload(onSave).ldap_tls_server_name).toBe('ldap.corp.example');
  });

  it('warns while verification is off and locks the CA file and server name fields', async () => {
    renderPanel({});

    await fireEvent.click(screen.getByLabelText('settings.ldap.tlsSkipVerify'));

    expect(screen.getByRole('alert').textContent).toContain('settings.ldap.tlsSkipVerifyWarning');
    expect((screen.getByLabelText('settings.ldap.caCertFile') as HTMLInputElement).disabled).toBe(
      true
    );
    expect(
      (screen.getByLabelText('settings.ldap.tlsServerName') as HTMLInputElement).disabled
    ).toBe(true);
  });

  it('defaults a config without the new fields to verifying with no CA file', () => {
    renderPanel({});

    expect((screen.getByLabelText('settings.ldap.tlsSkipVerify') as HTMLInputElement).checked).toBe(
      false
    );
    expect((screen.getByLabelText('settings.ldap.caCertFile') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('settings.ldap.tlsServerName') as HTMLInputElement).value).toBe(
      ''
    );
  });
});
