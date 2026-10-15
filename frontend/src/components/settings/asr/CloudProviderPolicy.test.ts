import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({ getProviderPolicy: vi.fn(), updateProviderPolicy: vi.fn() }));
vi.mock('$lib/api/asrProviderPolicy', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api/asrProviderPolicy')>()),
  getProviderPolicy: api.getProviderPolicy,
  updateProviderPolicy: api.updateProviderPolicy,
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});

import CloudProviderPolicy from './CloudProviderPolicy.svelte';

const POLICY = {
  providers: [
    {
      id: 'deepgram',
      display_name: 'Deepgram',
      allowed_for_users: true,
      shared_credentials: [
        {
          config_uuid: 'c-1',
          name: 'Team Deepgram',
          has_api_key: true,
          has_access_key_id: false,
          owner_name: 'Ada',
        },
      ],
    },
    {
      id: 'assemblyai',
      display_name: 'AssemblyAI',
      allowed_for_users: true,
      shared_credentials: [],
    },
  ],
  users_may_add_own_keys: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getProviderPolicy.mockResolvedValue(structuredClone(POLICY));
});

describe('CloudProviderPolicy', () => {
  it('makes no API call while locked and shows the locked state', () => {
    render(CloudProviderPolicy, { locked: true });
    expect(screen.getByTestId('asr-policy-locked')).toBeTruthy();
    expect(api.getProviderPolicy).not.toHaveBeenCalled();
    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('lists providers with their key flags and never a key value', async () => {
    render(CloudProviderPolicy, { locked: false });
    await screen.findByText('Deepgram');
    expect(screen.getByText('settings.asrPolicy.apiKeySet')).toBeTruthy();
    expect(screen.queryByText('settings.asrPolicy.accessKeySet')).toBeNull();
    expect(screen.getByText('Team Deepgram')).toBeTruthy();
    expect(screen.getByText('settings.asrPolicy.noShared')).toBeTruthy();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it('keeps Save disabled until something changes, then sends the exact body', async () => {
    api.updateProviderPolicy.mockResolvedValue(structuredClone(POLICY));
    render(CloudProviderPolicy, { locked: false });
    await screen.findByText('Deepgram');
    const save = screen.getByRole('button', {
      name: 'settings.asrPolicy.save',
    }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    await fireEvent.click(
      screen.getByRole('checkbox', { name: 'settings.asrPolicy.allowFor{"name":"AssemblyAI"}' })
    );
    await fireEvent.click(screen.getByRole('checkbox', { name: 'settings.asrPolicy.ownKeys' }));
    expect(save.disabled).toBe(false);
    await fireEvent.click(save);

    await waitFor(() =>
      expect(api.updateProviderPolicy).toHaveBeenCalledWith({
        allowed_for_users: ['deepgram'],
        users_may_add_own_keys: false,
      })
    );
    expect(toast.success).toHaveBeenCalledWith('settings.asrPolicy.saved');
  });

  it('sends null when every provider is allowed', async () => {
    const policy = structuredClone(POLICY);
    policy.providers[1].allowed_for_users = false;
    api.getProviderPolicy.mockResolvedValue(policy);
    api.updateProviderPolicy.mockResolvedValue(structuredClone(POLICY));
    render(CloudProviderPolicy, { locked: false });
    await screen.findByText('Deepgram');
    await fireEvent.click(
      screen.getByRole('checkbox', { name: 'settings.asrPolicy.allowFor{"name":"AssemblyAI"}' })
    );
    await fireEvent.click(screen.getByRole('button', { name: 'settings.asrPolicy.save' }));
    await waitFor(() =>
      expect(api.updateProviderPolicy).toHaveBeenCalledWith({
        allowed_for_users: null,
        users_may_add_own_keys: true,
      })
    );
  });

  it('shows a fixed sentence, not the server text, when saving fails', async () => {
    api.updateProviderPolicy.mockRejectedValue({
      response: {
        status: 422,
        data: { detail: { code: 'unknown_asr_provider', message: 'raw!' } },
      },
    });
    render(CloudProviderPolicy, { locked: false });
    await screen.findByText('Deepgram');
    await fireEvent.click(screen.getByRole('checkbox', { name: 'settings.asrPolicy.ownKeys' }));
    await fireEvent.click(screen.getByRole('button', { name: 'settings.asrPolicy.save' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('settings.asrPolicy.saveFailed'));
    expect(JSON.stringify(toast.error.mock.calls)).not.toContain('raw!');
  });

  it('asks the parent to open the credential editor for that provider', async () => {
    const seen: string[] = [];
    render(CloudProviderPolicy, {
      props: { locked: false },
      events: {
        addshared: (e: CustomEvent<{ providerId: string }>) => seen.push(e.detail.providerId),
      },
    });
    await screen.findByText('Deepgram');
    await fireEvent.click(
      screen.getByRole('button', { name: 'settings.asrPolicy.addSharedFor{"name":"AssemblyAI"}' })
    );
    expect(seen).toEqual(['assemblyai']);
  });
});
