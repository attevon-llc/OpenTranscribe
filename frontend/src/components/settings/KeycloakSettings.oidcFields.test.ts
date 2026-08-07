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

import KeycloakSettings from './KeycloakSettings.svelte';

/**
 * What `AuthenticationSettings` hands the panel for a configured provider.
 * `keycloak_client_secret` is `null`: the API never sends a secret's value.
 */
function storedConfig(overrides: Record<string, unknown> = {}) {
  return {
    keycloak_enabled: true,
    keycloak_server_url: 'https://auth.example.com',
    keycloak_discovery_url:
      'https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration',
    keycloak_realm: 'opentranscribe',
    keycloak_client_id: 'opentranscribe',
    keycloak_client_secret: null,
    keycloak_roles_claim: 'groups',
    keycloak_issuer: 'https://auth.example.com/application/o/opentranscribe/',
    keycloak_scopes: 'openid email profile offline_access',
    ...overrides,
  };
}

/** Render with a `save` listener attached, mirroring how the parent panel binds it. */
function renderPanel(props: Record<string, unknown>, onSave = vi.fn()) {
  render(KeycloakSettings, { props, events: { save: onSave } } as never);
  return onSave;
}

function clickSave() {
  return fireEvent.click(
    screen.getByRole('button', { name: 'settings.keycloak.saveConfiguration' })
  );
}

/** The payload the panel dispatched to its parent. */
function savedPayload(onSave: ReturnType<typeof vi.fn>): Record<string, unknown> {
  expect(onSave).toHaveBeenCalledTimes(1);
  return (onSave.mock.calls[0][0] as CustomEvent).detail as Record<string, unknown>;
}

/** Render, click Save, and return the payload dispatched to the parent. */
async function saveAndCapture(props: Record<string, unknown>) {
  const onSave = renderPanel(props);
  await clickSave();
  return savedPayload(onSave);
}

describe('Keycloak/OIDC panel — issue #353 discovery fields', () => {
  it('renders the four new fields with their stored values', () => {
    renderPanel({ config: storedConfig() });

    expect(screen.getByLabelText('settings.keycloak.discoveryUrl')).toHaveValue(
      'https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration'
    );
    expect(screen.getByLabelText('settings.keycloak.rolesClaim')).toHaveValue('groups');
    expect(screen.getByLabelText('settings.keycloak.scopes')).toHaveValue(
      'openid email profile offline_access'
    );
    expect(screen.getByLabelText('settings.keycloak.issuer')).toHaveValue(
      'https://auth.example.com/application/o/opentranscribe/'
    );
  });

  it('falls back to the backend defaults, not to empty strings', () => {
    renderPanel({ config: { keycloak_enabled: true } });

    expect(screen.getByLabelText('settings.keycloak.rolesClaim')).toHaveValue('realm_access.roles');
    expect(screen.getByLabelText('settings.keycloak.scopes')).toHaveValue('openid email profile');
    expect(screen.getByLabelText('settings.keycloak.discoveryUrl')).toHaveValue('');
  });

  it('submits the four new keys so the backend fix is reachable', async () => {
    const payload = await saveAndCapture({ config: storedConfig() });

    expect(payload).toMatchObject({
      keycloak_discovery_url:
        'https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration',
      keycloak_roles_claim: 'groups',
      keycloak_issuer: 'https://auth.example.com/application/o/opentranscribe/',
      keycloak_scopes: 'openid email profile offline_access',
    });
  });

  it('flags the realm as superseded once a discovery URL is set', async () => {
    renderPanel({ config: storedConfig({ keycloak_discovery_url: '' }) });

    expect(screen.queryByText('settings.keycloak.realmSupersededHelp')).not.toBeInTheDocument();

    await fireEvent.input(screen.getByLabelText('settings.keycloak.discoveryUrl'), {
      target: { value: 'https://auth.example.com/.well-known/openid-configuration' },
    });

    expect(screen.getByText('settings.keycloak.realmSupersededHelp')).toBeInTheDocument();
    // Never disabled — the admin may be mid-edit.
    expect(screen.getByLabelText('settings.keycloak.realm')).not.toBeDisabled();
  });
});

describe('Keycloak/OIDC panel — client secret is never echoed back', () => {
  it('renders empty with a keep-current hint when a secret is stored', () => {
    renderPanel({ config: storedConfig(), secretIsSet: true });

    const field = screen.getByLabelText('settings.keycloak.clientSecret');
    expect(field).toHaveValue('');
    expect(field).toHaveAttribute('placeholder', 'settings.keycloak.clientSecretKeepPlaceholder');
    expect(screen.getByText('settings.keycloak.clientSecretSetHelp')).toBeInTheDocument();
  });

  it('treats a null config_value as "a secret is stored" when is_set is unavailable', () => {
    // The parent's flattening currently drops `is_set`; null is the only signal left.
    renderPanel({ config: storedConfig() });

    expect(screen.getByText('settings.keycloak.clientSecretSetHelp')).toBeInTheDocument();
  });

  it('omits the secret from the payload when the admin did not type one', async () => {
    const payload = await saveAndCapture({ config: storedConfig(), secretIsSet: true });

    expect(payload).not.toHaveProperty('keycloak_client_secret');
  });

  it('sends the secret only when the admin actually types one', async () => {
    const onSave = renderPanel({ config: storedConfig(), secretIsSet: true });

    await fireEvent.input(screen.getByLabelText('settings.keycloak.clientSecret'), {
      target: { value: 'a-brand-new-secret' },
    });
    await clickSave();

    expect(savedPayload(onSave)).toMatchObject({
      keycloak_client_secret: 'a-brand-new-secret',
    });
  });

  it('offers the plain enter-a-secret hint when nothing is stored', () => {
    renderPanel({ config: { keycloak_enabled: true }, secretIsSet: false });

    expect(screen.getByText('settings.keycloak.clientSecretHelp')).toBeInTheDocument();
    expect(screen.getByLabelText('settings.keycloak.clientSecret')).toHaveAttribute(
      'placeholder',
      'settings.keycloak.enterClientSecret'
    );
  });
});
