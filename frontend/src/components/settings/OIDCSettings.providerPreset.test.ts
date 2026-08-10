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

import OIDCSettings from './OIDCSettings.svelte';

function renderPanel(props: Record<string, unknown> = { config: { oidc_enabled: true } }) {
  return render(OIDCSettings, { props } as never);
}

function selectPreset(id: string) {
  return fireEvent.change(screen.getByLabelText('settings.oidc.providerPreset'), {
    target: { value: id },
  });
}

describe('OIDC panel — provider presets (P1.1)', () => {
  it('fills roles claim, scopes and discovery URL for Authentik', async () => {
    renderPanel();

    await selectPreset('authentik');

    expect(screen.getByLabelText('settings.oidc.rolesClaim')).toHaveValue('groups');
    expect(screen.getByLabelText('settings.oidc.scopes')).toHaveValue('openid email profile');
    expect(screen.getByLabelText('settings.oidc.discoveryUrl')).toHaveValue(
      'https://authentik.example.com/application/o/<app-slug>/.well-known/openid-configuration'
    );
  });

  it('shows the Authentik email_verified caveat only while that preset is selected', async () => {
    renderPanel();

    expect(screen.queryByText('settings.oidc.presetNoteAuthentik')).not.toBeInTheDocument();

    await selectPreset('authentik');
    expect(screen.getByText('settings.oidc.presetNoteAuthentik')).toBeInTheDocument();

    await selectPreset('keycloak');
    expect(screen.queryByText('settings.oidc.presetNoteAuthentik')).not.toBeInTheDocument();
  });

  it('leaves the discovery URL untouched for Keycloak, which resolves via realm', async () => {
    renderPanel({
      config: {
        oidc_enabled: true,
        oidc_discovery_url: 'https://existing.example.com/.well-known/openid-configuration',
      },
    });

    await selectPreset('keycloak');

    expect(screen.getByLabelText('settings.oidc.discoveryUrl')).toHaveValue(
      'https://existing.example.com/.well-known/openid-configuration'
    );
    expect(screen.getByLabelText('settings.oidc.rolesClaim')).toHaveValue('realm_access.roles');
  });

  it('fills Entra with the roles claim (not groups, which carry GUIDs) and its overage caveat', async () => {
    renderPanel();

    await selectPreset('entra');

    expect(screen.getByLabelText('settings.oidc.rolesClaim')).toHaveValue('roles');
    expect(screen.getByText('settings.oidc.presetNoteEntra')).toBeInTheDocument();
  });

  it('fills Google Workspace with an empty roles claim and its no-groups caveat', async () => {
    renderPanel();

    await selectPreset('google');

    expect(screen.getByLabelText('settings.oidc.rolesClaim')).toHaveValue('');
    expect(screen.getByText('settings.oidc.presetNoteGoogle')).toBeInTheDocument();
  });

  it('still lets the admin edit every field after applying a preset', async () => {
    renderPanel();

    await selectPreset('okta');
    await fireEvent.input(screen.getByLabelText('settings.oidc.rolesClaim'), {
      target: { value: 'custom.path' },
    });

    expect(screen.getByLabelText('settings.oidc.rolesClaim')).toHaveValue('custom.path');
  });
});
