/**
 * The deployment-wide privacy switches (issue #1280, super_admin). Per-switch partial PUTs, a
 * purge that is offered only once the switch is off and carries the chosen scope, and the
 * "everything" scope offered only where the deployment is KNOWN to be single-tenant (the server
 * refuses it in multi-tenant mode, and an unknown mode must fail closed).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      en[key] ?? key
    );
  return { t: readable(t), locale: readable('en') };
});

const api = vi.hoisted(() => ({
  getInstance: vi.fn(),
  updateInstance: vi.fn(),
  purgeInstance: vi.fn(),
  getPurgeStatus: vi.fn(),
}));
vi.mock('$lib/api/privacy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/privacy')>();
  return { ...actual, PrivacyApi: api };
});
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import InstancePrivacyCard from './InstancePrivacyCard.svelte';
import { capabilities } from '$stores/capabilities';

function setMode(tenancyMode: 'single' | 'multi' | undefined) {
  capabilities.set({
    edition: 'community',
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
    tenancyMode,
  });
}

const sw = (id: string) => screen.getByTestId(`${id}-switch`) as HTMLInputElement;
const purge = (id: string) => screen.getByTestId(`${id}-purge`) as HTMLButtonElement;

async function confirmPurge() {
  await fireEvent.click(screen.getByRole('checkbox', { name: /I understand/ }));
  await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'PURGE' } });
  await fireEvent.click(screen.getByTestId('purge-confirm'));
}

beforeEach(() => {
  vi.clearAllMocks();
  setMode('single');
  api.getInstance.mockResolvedValue({
    voiceprint_matching_enabled: true,
    gender_inference_enabled: true,
  });
  api.getPurgeStatus.mockResolvedValue({ task_id: 't', status: 'completed', counts: {} });
});

describe('InstancePrivacyCard', () => {
  it('loads both switches from the instance layer', async () => {
    render(InstancePrivacyCard);
    await screen.findByTestId('instance-voiceprints-switch');
    expect(api.getInstance).toHaveBeenCalledTimes(1);
    expect(sw('instance-voiceprints').checked).toBe(true);
    expect(sw('instance-gender').checked).toBe(true);
  });

  it('tells operators gender is inferred automatically, on by default, with an off switch', async () => {
    render(InstancePrivacyCard);
    expect(await screen.findByTestId('instance-gender-notice')).toHaveTextContent(
      'inferred automatically'
    );
  });

  it('PUTs only the switch that moved', async () => {
    api.updateInstance.mockResolvedValue({
      voiceprint_matching_enabled: true,
      gender_inference_enabled: false,
    });
    render(InstancePrivacyCard);
    await screen.findByTestId('instance-gender-switch');

    await fireEvent.click(sw('instance-gender'));

    await waitFor(() =>
      expect(api.updateInstance).toHaveBeenCalledWith({ gender_inference_enabled: false })
    );
    await waitFor(() => expect(sw('instance-gender').checked).toBe(false));
    expect(sw('instance-voiceprints').checked).toBe(true);
  });

  it('keeps the switch on a failed save and says so', async () => {
    api.updateInstance.mockRejectedValue(new Error('500'));
    render(InstancePrivacyCard);
    await screen.findByTestId('instance-voiceprints-switch');
    await fireEvent.click(sw('instance-voiceprints'));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(sw('instance-voiceprints').checked).toBe(true);
  });

  it('disables deletion while a switch is on and enables it once off', async () => {
    api.getInstance.mockResolvedValue({
      voiceprint_matching_enabled: false,
      gender_inference_enabled: true,
    });
    render(InstancePrivacyCard);
    await screen.findByTestId('instance-voiceprints-switch');
    expect(purge('instance-voiceprints')).toBeEnabled();
    expect(purge('instance-gender')).toBeDisabled();
  });

  it('sends the personal scope by default and never deletes without the typed word', async () => {
    api.getInstance.mockResolvedValue({
      voiceprint_matching_enabled: false,
      gender_inference_enabled: true,
    });
    api.purgeInstance.mockResolvedValue({ task_id: 't' });
    render(InstancePrivacyCard);
    await screen.findByTestId('instance-voiceprints-switch');

    await fireEvent.click(purge('instance-voiceprints'));
    expect(screen.getByTestId('purge-confirm')).toBeDisabled();
    expect(api.purgeInstance).not.toHaveBeenCalled();
    await confirmPurge();

    await waitFor(() =>
      expect(api.purgeInstance).toHaveBeenCalledWith('personal', ['voiceprints'])
    );
  });

  it('sends the everything scope only when the operator chose it on a single-tenant deployment', async () => {
    api.getInstance.mockResolvedValue({
      voiceprint_matching_enabled: true,
      gender_inference_enabled: false,
    });
    api.purgeInstance.mockResolvedValue({ task_id: 't' });
    render(InstancePrivacyCard);
    await screen.findByTestId('instance-gender-switch');

    await fireEvent.click(screen.getByLabelText('Everything on this deployment'));
    await fireEvent.click(purge('instance-gender'));
    await confirmPurge();

    await waitFor(() => expect(api.purgeInstance).toHaveBeenCalledWith('all', ['inferred_gender']));
  });

  it.each(['multi', undefined] as const)(
    'does not offer the everything scope when the mode is %s',
    async (mode) => {
      setMode(mode);
      render(InstancePrivacyCard);
      await screen.findByTestId('instance-voiceprints-switch');
      expect(screen.queryByLabelText('Everything on this deployment')).toBeNull();
      expect(screen.getByLabelText('The personal workspaces of all users')).toBeChecked();
    }
  );

  it('explains why in multi-tenant mode', async () => {
    setMode('multi');
    render(InstancePrivacyCard);
    expect(await screen.findByTestId('instance-scope-multi')).toHaveTextContent(
      'Organization administrators delete their own'
    );
  });

  it('shows the refusal when the server rejects the scope anyway', async () => {
    api.getInstance.mockResolvedValue({
      voiceprint_matching_enabled: false,
      gender_inference_enabled: true,
    });
    api.purgeInstance.mockRejectedValue({
      response: { status: 403, data: { detail: { code: 'purge_scope_not_allowed' } } },
    });
    render(InstancePrivacyCard);
    await screen.findByTestId('instance-voiceprints-switch');
    await fireEvent.click(purge('instance-voiceprints'));
    await confirmPurge();
    expect(await screen.findByRole('alert')).toHaveTextContent('not allowed on a multi-tenant');
  });
});
