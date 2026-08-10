/**
 * The designation control is the only place an operator learns that auth mail
 * is broken. The backend resolves the designation on every send and degrades to
 * env SMTP with an ERROR log when the designated config was deleted or disabled
 * — invisible from the UI unless this panel says so. These tests pin that it
 * does, that it never offers a config the API would reject, and that a rejection
 * reaches the operator with the server's own wording.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  // Identity translator so assertions can match on the i18n key.
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));

import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import AuthMailDesignation from './AuthMailDesignation.svelte';

const get = vi.mocked(axiosInstance.get);
const put = vi.mocked(axiosInstance.put);

const ENABLED_UUID = '019ec90a-1b2c-7def-8000-00000000ee01';
const DISABLED_UUID = '019ec90a-1b2c-7def-8000-00000000ee02';
const GONE_UUID = '019ec90a-1b2c-7def-8000-0000000000ff';

const CONFIGS = [
  { uuid: ENABLED_UUID, name: 'Corp M365', provider: 'm365', is_enabled: true },
  { uuid: DISABLED_UUID, name: 'Old relay', provider: 'smtp', is_enabled: false },
];

function designation(overrides: Record<string, unknown> = {}) {
  return {
    config_uuid: null,
    config_name: null,
    provider: null,
    is_enabled: null,
    resolves: false,
    status: 'not_designated',
    env_smtp_configured: true,
    ...overrides,
  };
}

async function renderPanel(current: Record<string, unknown>, configs = CONFIGS) {
  get.mockResolvedValue({ data: current } as never);
  render(AuthMailDesignation, { props: { configs } } as never);
  await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());
  return screen.getByRole('combobox') as HTMLSelectElement;
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'common.save' }) as HTMLButtonElement;
}

describe('AuthMailDesignation', () => {
  beforeEach(() => vi.clearAllMocks());

  it('reads the designation from the auth-config surface, not the watch-source one', async () => {
    await renderPanel(designation());

    expect(get).toHaveBeenCalledWith('/admin/auth-config/email/designation');
  });

  it('preselects the config currently carrying auth mail', async () => {
    const select = await renderPanel(
      designation({
        config_uuid: ENABLED_UUID,
        config_name: 'Corp M365',
        provider: 'm365',
        is_enabled: true,
        resolves: true,
        status: 'active',
      })
    );

    expect(select.value).toBe(ENABLED_UUID);
    expect(screen.getByText('settings.authentication.authMail.active')).toBeInTheDocument();
  });

  it('offers only enabled configs, since the API rejects the rest', async () => {
    const select = await renderPanel(designation());

    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toEqual(['', ENABLED_UUID]);
    expect(values).not.toContain(DISABLED_UUID);
  });

  it('keeps Save inert until the selection actually changes', async () => {
    const select = await renderPanel(designation({ config_uuid: ENABLED_UUID, status: 'active' }));

    expect(saveButton()).toBeDisabled();

    await fireEvent.change(select, { target: { value: '' } });

    expect(saveButton()).toBeEnabled();
  });

  it('designates the chosen config', async () => {
    const select = await renderPanel(designation());
    put.mockResolvedValue({
      data: designation({ config_uuid: ENABLED_UUID, config_name: 'Corp M365', status: 'active' }),
    } as never);

    await fireEvent.change(select, { target: { value: ENABLED_UUID } });
    await fireEvent.click(saveButton());

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/admin/auth-config/email/designation', {
        config_uuid: ENABLED_UUID,
      })
    );
    expect(toastStore.success).toHaveBeenCalledWith('settings.authentication.authMail.saved');
  });

  it('clears the designation with an empty string, which means "use env SMTP"', async () => {
    const select = await renderPanel(designation({ config_uuid: ENABLED_UUID, status: 'active' }));
    put.mockResolvedValue({ data: designation() } as never);

    await fireEvent.change(select, { target: { value: '' } });
    await fireEvent.click(saveButton());

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/admin/auth-config/email/designation', {
        config_uuid: '',
      })
    );
    expect(toastStore.success).toHaveBeenCalledWith('settings.authentication.authMail.cleared');
  });

  it('warns when the designated config was deleted, and does not read as "none"', async () => {
    const select = await renderPanel(designation({ config_uuid: GONE_UUID, status: 'missing' }));

    expect(
      screen.getByText('settings.authentication.authMail.danglingMissing')
    ).toBeInTheDocument();
    // Without the stranded option the select would fall back to the "not
    // designated" entry and hide the very problem being reported.
    expect(select.value).toBe(GONE_UUID);
  });

  it('warns when the designated config was disabled', async () => {
    await renderPanel(
      designation({
        config_uuid: DISABLED_UUID,
        config_name: 'Old relay',
        is_enabled: false,
        status: 'disabled',
      })
    );

    expect(
      screen.getByText('settings.authentication.authMail.danglingDisabled')
    ).toBeInTheDocument();
  });

  it('escalates when neither the designation nor env SMTP can deliver', async () => {
    await renderPanel(designation({ env_smtp_configured: false }));

    expect(screen.getByText('settings.authentication.authMail.noTransport')).toBeInTheDocument();
  });

  it('stays quiet while a working designation is in place', async () => {
    await renderPanel(
      designation({
        config_uuid: ENABLED_UUID,
        config_name: 'Corp M365',
        resolves: true,
        status: 'active',
        env_smtp_configured: false,
      })
    );

    expect(
      screen.queryByText('settings.authentication.authMail.noTransport')
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText('settings.authentication.authMail.danglingMissing')
    ).not.toBeInTheDocument();
  });

  it("surfaces the API's refusal verbatim rather than a generic failure", async () => {
    const select = await renderPanel(designation());
    put.mockRejectedValue({
      response: { status: 400, data: { detail: `No email configuration with UUID ${GONE_UUID}` } },
    } as never);

    await fireEvent.change(select, { target: { value: ENABLED_UUID } });
    await fireEvent.click(saveButton());

    await waitFor(() =>
      expect(toastStore.error).toHaveBeenCalledWith(`No email configuration with UUID ${GONE_UUID}`)
    );
  });
});
