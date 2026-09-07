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

/**
 * The generated callback URL is the `redirect_uri` the identity provider navigates the
 * BROWSER to, and the same string is replayed at token exchange. It must therefore be a
 * page the SPA serves. The backend handler at `/api/auth/oidc/callback` returns a
 * `JSONResponse` and never redirects, so registering it authenticates the user and then
 * leaves them looking at a JSON document. Nothing pinned this value before, and the panel
 * shipped generating exactly that broken form.
 */
function renderPanel(overrides: Record<string, unknown> = {}) {
  render(OIDCSettings, {
    props: { config: { oidc_enabled: true, ...overrides } },
  } as never);
  return screen.getByLabelText('settings.oidc.callbackUrl') as HTMLInputElement;
}

function clickAutoDetect() {
  return fireEvent.click(screen.getByRole('button', { name: 'settings.oidc.autoDetect' }));
}

describe('OIDC panel — Auto-detect generates the SPA login page', () => {
  it('fills the field with the frontend /login route on this origin', async () => {
    const field = renderPanel();

    await clickAutoDetect();

    expect(field.value).toBe(`${window.location.origin}/login`);
    expect(new URL(field.value).pathname).toBe('/login');
  });

  it('never generates the backend API callback route', async () => {
    const field = renderPanel();

    await clickAutoDetect();

    // The exact value this panel used to produce.
    expect(field.value).not.toBe(`${window.location.origin}/api/auth/oidc/callback`);
    expect(field.value).not.toContain('/api/');
  });

  it('submits the generated value, so the fix reaches the backend', async () => {
    const onSave = vi.fn();
    render(OIDCSettings, {
      props: { config: { oidc_enabled: true } },
      events: { save: onSave },
    } as never);

    await clickAutoDetect();
    await fireEvent.click(screen.getByRole('button', { name: 'settings.oidc.saveConfiguration' }));

    const payload = (onSave.mock.calls[0][0] as CustomEvent).detail as Record<string, unknown>;
    expect(payload.oidc_callback_url).toBe(`${window.location.origin}/login`);
  });
});

describe('OIDC panel — a hand-typed callback URL is warned about, never blocked', () => {
  it('warns loudly when the value is a backend /api/ route', async () => {
    const field = renderPanel();

    await fireEvent.input(field, {
      target: { value: 'https://app.example.com/api/auth/oidc/callback' },
    });

    expect(screen.getByText('settings.oidc.callbackUrlApiWarning')).toBeInTheDocument();
    // A warning, not a block: the admin's value is still there and still submittable.
    expect(field).not.toBeDisabled();
    expect(field.value).toBe('https://app.example.com/api/auth/oidc/callback');
  });

  it('stays silent for the auto-detected value', async () => {
    renderPanel();

    await clickAutoDetect();

    expect(screen.queryByText('settings.oidc.callbackUrlApiWarning')).not.toBeInTheDocument();
    expect(screen.queryByText('settings.oidc.callbackUrlPathWarning')).not.toBeInTheDocument();
  });

  it('stays silent for an SPA served from a sub-path behind a reverse proxy', async () => {
    const field = renderPanel();

    await fireEvent.input(field, {
      target: { value: 'https://apps.example.com/opentranscribe/login' },
    });

    expect(screen.queryByText('settings.oidc.callbackUrlApiWarning')).not.toBeInTheDocument();
    expect(screen.queryByText('settings.oidc.callbackUrlPathWarning')).not.toBeInTheDocument();
  });

  it('offers the softer note for an unrecognised path, without blocking it', async () => {
    const field = renderPanel();

    await fireEvent.input(field, { target: { value: 'https://app.example.com/signin' } });

    expect(screen.getByText('settings.oidc.callbackUrlPathWarning')).toBeInTheDocument();
    expect(screen.queryByText('settings.oidc.callbackUrlApiWarning')).not.toBeInTheDocument();
  });

  it('says nothing while the field is empty or half-typed', async () => {
    const field = renderPanel();

    expect(screen.queryByText('settings.oidc.callbackUrlPathWarning')).not.toBeInTheDocument();

    await fireEvent.input(field, { target: { value: 'https:/' } });

    expect(screen.queryByText('settings.oidc.callbackUrlPathWarning')).not.toBeInTheDocument();
    expect(screen.queryByText('settings.oidc.callbackUrlApiWarning')).not.toBeInTheDocument();
  });
});
