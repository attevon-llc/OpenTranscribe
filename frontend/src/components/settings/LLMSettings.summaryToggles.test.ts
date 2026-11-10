/**
 * The two auto-summary switches at the bottom of the LLM panel: the per-user preference
 * (`PUT /user-settings/ai-summary`, JSON body) and the admin-only deployment-wide kill switch
 * (`PUT /admin/system/ai-summary`).
 *
 * The system route declares `enabled` as a bare `bool` after a keyword-only marker, which
 * FastAPI reads as a QUERY parameter (backend/tests/api/endpoints/test_admin_ai_summary_routes.py
 * says so in as many words). The panel used to send it as a JSON body, so the server answered
 * 422 and the admin's switch silently never worked.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';
import type { ProviderDefaults, UserLLMSettings } from '$lib/api/llmSettings';

const api = vi.hoisted(() => ({
  getSupportedProviders: vi.fn(),
  getUserConfigurations: vi.fn(),
}));
vi.mock('$lib/api/llmSettings', async () => {
  const actual =
    await vi.importActual<typeof import('$lib/api/llmSettings')>('$lib/api/llmSettings');
  return { ...actual, LLMSettingsApi: { ...actual.LLMSettingsApi, ...api } };
});

const http = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: http }));

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

vi.mock('$lib/services/llmService', () => ({
  llmService: { getStatus: vi.fn().mockResolvedValue({ available: true }) },
}));

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import LLMSettings from './LLMSettings.svelte';

const OPENAI = {
  provider: 'openai',
  default_model: 'm',
  requires_api_key: true,
} as ProviderDefaults;
const SAVED = {
  uuid: 'cfg-1',
  name: 'My OpenAI',
  provider: 'openai',
  model_name: 'm',
  is_shared: false,
  test_status: 'untested',
} as unknown as UserLLMSettings;

let systemEnabled = true;

beforeEach(() => {
  vi.clearAllMocks();
  systemEnabled = true;
  api.getSupportedProviders.mockResolvedValue({ providers: [OPENAI] });
  api.getUserConfigurations.mockResolvedValue({
    configurations: [SAVED],
    shared_configurations: [],
    active_configuration_id: SAVED.uuid,
    total: 1,
  });
  http.get.mockImplementation(async (url: string) => ({
    data: url.includes('admin')
      ? { ai_summary_enabled: systemEnabled }
      : { ai_summary_enabled: true, system_enabled: systemEnabled },
  }));
  http.put.mockImplementation(
    async (url: string, _body: unknown, config?: { params?: { enabled?: boolean } }) => {
      if (url.includes('admin')) {
        // Mirrors the route: `enabled` is read from the query string only.
        if (config?.params?.enabled === undefined) {
          throw Object.assign(new Error('422'), {
            response: { status: 422, data: { detail: 'enabled required' } },
          });
        }
        systemEnabled = config.params.enabled;
        return { data: { ai_summary_enabled: systemEnabled, scope: 'system' } };
      }
      return {
        data: { ai_summary_enabled: (_body as { enabled: boolean }).enabled, message: 'saved' },
      };
    }
  );
});

const switches = (c: HTMLElement) => [
  ...c.querySelectorAll<HTMLInputElement>('.summary-toggle-row [role="switch"]'),
];

describe('auto-summary switches', () => {
  it('shows the system switch to an admin only', async () => {
    const admin = render(LLMSettings, { props: { isAdmin: true } });
    await waitFor(() => expect(switches(admin.container)).toHaveLength(2));
    admin.unmount();
    http.get.mockClear();
    const user = render(LLMSettings, { props: { isAdmin: false } });
    await waitFor(() => expect(switches(user.container)).toHaveLength(1));
    expect(http.get).toHaveBeenCalledWith('/user-settings/ai-summary');
  });

  it('saves the personal preference as a JSON body', async () => {
    const { container } = render(LLMSettings, { props: { isAdmin: false } });
    await waitFor(() => expect(switches(container)).toHaveLength(1));
    await fireEvent.click(switches(container)[0]);
    await waitFor(() =>
      expect(http.put).toHaveBeenCalledWith('/user-settings/ai-summary', { enabled: false })
    );
    expect(toast.success).toHaveBeenCalledWith('saved', 3000);
  });

  it('saves the deployment-wide switch as the query parameter the route reads', async () => {
    const { container } = render(LLMSettings, { props: { isAdmin: true } });
    await waitFor(() => expect(switches(container)).toHaveLength(2));
    await fireEvent.click(switches(container)[1]);
    await waitFor(() => expect(systemEnabled).toBe(false));
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalledWith('settings.llm.systemSummary: common.disabled', 3000);
    expect(switches(container)[1].checked).toBe(false);
  });

  it('rolls the switch back and says so when the server refuses', async () => {
    const { container } = render(LLMSettings, { props: { isAdmin: true } });
    await waitFor(() => expect(switches(container)).toHaveLength(2));
    http.put.mockRejectedValueOnce(
      Object.assign(new Error('boom'), { response: { status: 500, data: { detail: 'nope' } } })
    );
    await fireEvent.click(switches(container)[1]);
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(switches(container)[1].checked).toBe(true);
  });

  it('tells a plain user the deployment switch is off, and disables their own switch', async () => {
    systemEnabled = false;
    const { container } = render(LLMSettings, { props: { isAdmin: false } });
    await waitFor(() => expect(switches(container)).toHaveLength(1));
    await waitFor(() => expect(switches(container)[0].disabled).toBe(true));
    expect(container.querySelector('.summary-hint.warning')?.textContent?.trim()).toBe(
      'settings.llm.systemSummaryDisabledHint'
    );
    expect(http.get).toHaveBeenCalledWith('/user-settings/ai-summary');
  });

  it('leaves a plain user their switch while the deployment switch is on', async () => {
    const { container } = render(LLMSettings, { props: { isAdmin: false } });
    await waitFor(() => expect(switches(container)).toHaveLength(1));
    expect(switches(container)[0].disabled).toBe(false);
    expect(container.querySelector('.summary-hint.warning')).toBeNull();
  });

  it('keeps an admin able to switch their own preference when the deployment switch is off', async () => {
    systemEnabled = false;
    const { container } = render(LLMSettings, { props: { isAdmin: true } });
    await waitFor(() => expect(switches(container)).toHaveLength(2));
    await waitFor(() => expect(switches(container)[1].checked).toBe(false));
    expect(switches(container)[0].disabled).toBe(false);
  });
});
