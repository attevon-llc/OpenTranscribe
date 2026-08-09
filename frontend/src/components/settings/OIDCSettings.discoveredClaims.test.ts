import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import { vi } from 'vitest';

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

const baseConfig = { oidc_enabled: true, oidc_roles_claim: 'groups' };

function renderPanel(testResult?: Record<string, unknown>) {
  return render(OIDCSettings, {
    props: { config: baseConfig, testResult },
  } as never);
}

describe('OIDC panel — discovered claims (P1.2)', () => {
  it('renders nothing when no test has been run yet', () => {
    renderPanel();

    expect(screen.queryByText('settings.oidc.discoveredClaims')).not.toBeInTheDocument();
  });

  it('renders nothing for a failed test — there is no discovery document to show', () => {
    renderPanel({ success: false, message: 'nope' });

    expect(screen.queryByText('settings.oidc.discoveredClaims')).not.toBeInTheDocument();
  });

  it('lists the advertised claim names as tags after a successful test', () => {
    renderPanel({
      success: true,
      message: 'ok',
      details: {
        claims_supported: ['sub', 'email', 'groups'],
        configured_roles_claim: 'groups',
        roles_claim_advertised: 'yes',
      },
    });

    expect(screen.getByText('settings.oidc.discoveredClaims')).toBeInTheDocument();
    expect(screen.getByText('sub')).toBeInTheDocument();
    expect(screen.getByText('email')).toBeInTheDocument();
    expect(screen.getByText('groups')).toBeInTheDocument();
    expect(screen.getByText('settings.oidc.rolesClaimAdvertisedYes')).toBeInTheDocument();
  });

  it('shows the "no" caveat when the configured roles claim is not advertised', () => {
    renderPanel({
      success: true,
      message: 'ok',
      details: {
        claims_supported: ['sub', 'email'],
        configured_roles_claim: 'groups',
        roles_claim_advertised: 'no',
      },
    });

    expect(screen.getByText('settings.oidc.rolesClaimAdvertisedNo')).toBeInTheDocument();
  });

  it('shows the "unknown" caveat and the no-list message when the provider is silent', () => {
    renderPanel({
      success: true,
      message: 'ok',
      details: {
        claims_supported: null,
        configured_roles_claim: 'groups',
        roles_claim_advertised: 'unknown',
      },
    });

    expect(screen.getByText('settings.oidc.noClaimsSupported')).toBeInTheDocument();
    expect(screen.getByText('settings.oidc.rolesClaimAdvertisedUnknown')).toBeInTheDocument();
  });
});
