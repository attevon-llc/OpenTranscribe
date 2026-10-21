/**
 * The attention dot on the "Auth Email" tab (issue #1240).
 *
 * The tab's panel is a lazy chunk that only loads when it is opened, so a dot that waited
 * for the panel would not appear until the admin had already clicked into the problem. The
 * strip therefore reads the readiness itself on mount, and takes the panel's own saves
 * back through `on:change` so the dot clears the moment it is fixed.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/api/authConfig', () => ({
  AuthConfigApi: {
    getAllConfigs: vi.fn(),
    updateCategory: vi.fn(),
    testConnection: vi.fn(),
    getAuthMailReadiness: vi.fn(),
    getAuthMailDesignation: vi.fn(),
    setAuthMailDesignation: vi.fn(),
  },
}));

vi.mock('$lib/api/watchSourcesApi', () => ({
  getEmailConfigs: vi.fn(),
}));

vi.mock('$lib/passwordPolicy', () => ({
  fetchPasswordPolicy: vi.fn().mockRejectedValue(new Error('offline')),
}));

vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { AuthConfigApi } from '$lib/api/authConfig';
import { getEmailConfigs } from '$lib/api/watchSourcesApi';
import AuthenticationSettings from './AuthenticationSettings.svelte';
import { clearSectionData } from '$lib/settings/sectionData';
import { authTabLoaders } from '$lib/settings/sectionLoaders';

const UUID = '019ec90a-1b2c-7def-8000-00000000ee01';
const DOT = { name: 'authMailNotice.tabAttention' };

function answer(overrides: Record<string, unknown> = {}) {
  return {
    config_uuid: null,
    config_name: null,
    provider: null,
    is_enabled: null,
    resolves: false,
    status: 'not_designated',
    env_smtp_configured: false,
    deliverable: false,
    verification_required: false,
    ...overrides,
  };
}

describe('Auth Email tab attention dot', () => {
  beforeAll(async () => {
    await authTabLoaders.local();
    await authTabLoaders.mail();
  }, 120_000);

  beforeEach(() => {
    vi.clearAllMocks();
    clearSectionData();
    vi.mocked(AuthConfigApi.getAllConfigs).mockResolvedValue({ local: [] } as never);
    vi.mocked(getEmailConfigs).mockResolvedValue([
      { uuid: UUID, name: 'Corp relay', provider: 'smtp', is_enabled: true },
    ] as never);
  });

  it('appears on the tab without opening it when auth mail cannot be delivered', async () => {
    vi.mocked(AuthConfigApi.getAuthMailReadiness).mockResolvedValue(answer() as never);
    render(AuthenticationSettings);

    const dot = await screen.findByRole('img', DOT, { timeout: 5000 });
    expect(
      screen.getByRole('tab', { name: /settings\.authentication\.tab\.mail/ })
    ).toContainElement(dot);
    // The panel was never opened: nothing asked for the designation itself.
    expect(AuthConfigApi.getAuthMailDesignation).not.toHaveBeenCalled();
  });

  it.each([
    ['deliverable', answer({ deliverable: true })],
    ['unreadable (403/404/network)', null],
  ])('does not appear when the answer is %s', async (_name, readiness) => {
    vi.mocked(AuthConfigApi.getAuthMailReadiness).mockResolvedValue(readiness as never);
    render(AuthenticationSettings);

    await screen.findByRole('tab', { name: /settings\.authentication\.tab\.mail/ });
    await waitFor(() => expect(AuthConfigApi.getAuthMailReadiness).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByRole('img', DOT)).toBeNull();
  });

  it('clears as soon as the panel saves a designation that fixes it', async () => {
    vi.mocked(AuthConfigApi.getAuthMailReadiness).mockResolvedValue(answer() as never);
    vi.mocked(AuthConfigApi.getAuthMailDesignation).mockResolvedValue(answer() as never);
    vi.mocked(AuthConfigApi.setAuthMailDesignation).mockResolvedValue(
      answer({
        config_uuid: UUID,
        config_name: 'Corp relay',
        resolves: true,
        status: 'active',
        deliverable: true,
      }) as never
    );
    render(AuthenticationSettings);
    await screen.findByRole('img', DOT, { timeout: 5000 });

    await fireEvent.click(screen.getByRole('tab', { name: /settings\.authentication\.tab\.mail/ }));
    const select = (await screen.findByRole(
      'combobox',
      {},
      { timeout: 5000 }
    )) as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: UUID } });
    await fireEvent.click(screen.getByRole('button', { name: 'common.save' }));

    await waitFor(() => expect(screen.queryByRole('img', DOT)).toBeNull());
  });
});
