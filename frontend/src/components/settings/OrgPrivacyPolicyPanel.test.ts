/**
 * The organization privacy panel (issue #1280). The server decides who may turn what on; the
 * panel must render a layer-above lock as a disabled switch with the reason, send exactly the
 * one field the administrator changed, offer deletion only once the switch is off, and never
 * delete without the typed confirmation.
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
  getPolicy: vi.fn(),
  updatePolicy: vi.fn(),
  purgePolicyData: vi.fn(),
}));
vi.mock('$lib/api/orgAdmin', () => ({ OrgAdminApi: api }));
const purgeStatus = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/privacy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/privacy')>();
  return { ...actual, PrivacyApi: { getPurgeStatus: purgeStatus } };
});
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import OrgPrivacyPolicyPanel from './OrgPrivacyPolicyPanel.svelte';

type Layer = 'platform' | 'instance' | 'organization' | null;

const policy = (
  over: {
    voice?: boolean;
    gender?: boolean;
    voiceLocked?: Layer;
    genderLocked?: Layer;
  } = {}
) => {
  const voice = over.voice ?? true;
  const gender = over.gender ?? true;
  return {
    voiceprint_matching_enabled: voice,
    gender_inference_enabled: gender,
    instance: {
      voiceprint_matching_enabled: over.voiceLocked !== 'instance',
      gender_inference_enabled: over.genderLocked !== 'instance',
    },
    effective: {
      voiceprint_matching: {
        enabled: voice && !over.voiceLocked,
        locked_by: over.voiceLocked ?? (voice ? null : 'organization'),
      },
      gender_inference: {
        enabled: gender && !over.genderLocked,
        locked_by: over.genderLocked ?? (gender ? null : 'organization'),
      },
    },
    updated_at: null,
  };
};

const sw = (id: string) => screen.getByTestId(`${id}-switch`) as HTMLInputElement;
const purgeBtn = (id: string) => screen.getByTestId(`${id}-purge`) as HTMLButtonElement;

beforeEach(() => {
  vi.clearAllMocks();
  api.getPolicy.mockResolvedValue(policy());
  purgeStatus.mockResolvedValue({ task_id: 't-9', status: 'completed', counts: { docs: 1 } });
});

describe('OrgPrivacyPolicyPanel', () => {
  it('shows both features on by default, each with its switch', async () => {
    render(OrgPrivacyPolicyPanel);
    await screen.findByTestId('org-voiceprints-switch');
    expect(sw('org-voiceprints').checked).toBe(true);
    expect(sw('org-gender').checked).toBe(true);
    expect(sw('org-voiceprints')).toBeEnabled();
    expect(sw('org-voiceprints')).toHaveAttribute('role', 'switch');
  });

  it('tells people gender is inferred automatically and can be switched off', async () => {
    render(OrgPrivacyPolicyPanel);
    expect(await screen.findByTestId('org-gender-notice')).toHaveTextContent(
      "Gender is inferred automatically from each speaker's voice. It is on by default"
    );
  });

  it('renders a switch the deployment turned off as disabled, with who turned it off', async () => {
    api.getPolicy.mockResolvedValue(policy({ voiceLocked: 'instance' }));
    render(OrgPrivacyPolicyPanel);
    await screen.findByTestId('org-voiceprints-switch');
    expect(sw('org-voiceprints')).toBeDisabled();
    expect(screen.getByTestId('org-voiceprints-locked')).toHaveTextContent(
      'Turned off by the deployment operator'
    );
    expect(sw('org-gender')).toBeEnabled();
    expect(screen.queryByTestId('org-gender-locked')).toBeNull();
  });

  it('names the platform when the platform layer is what is off', async () => {
    api.getPolicy.mockResolvedValue(policy({ genderLocked: 'platform' }));
    render(OrgPrivacyPolicyPanel);
    await screen.findByTestId('org-gender-switch');
    expect(sw('org-gender')).toBeDisabled();
    expect(screen.getByTestId('org-gender-locked')).toHaveTextContent('platform operator');
  });

  it('does not lock the switch for the organization own off state', async () => {
    api.getPolicy.mockResolvedValue(policy({ voice: false }));
    render(OrgPrivacyPolicyPanel);
    await screen.findByTestId('org-voiceprints-switch');
    expect(sw('org-voiceprints')).toBeEnabled();
    expect(sw('org-voiceprints').checked).toBe(false);
  });

  it('sends only the changed field and shows the server answer', async () => {
    api.updatePolicy.mockResolvedValue(policy({ voice: false }));
    render(OrgPrivacyPolicyPanel);
    await screen.findByTestId('org-voiceprints-switch');

    await fireEvent.click(sw('org-voiceprints'));

    await waitFor(() =>
      expect(api.updatePolicy).toHaveBeenCalledWith({ voiceprint_matching_enabled: false })
    );
    expect(api.updatePolicy).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(sw('org-voiceprints').checked).toBe(false));
    expect(toast.success).toHaveBeenCalled();
  });

  it('leaves the switch where the server has it when the save fails', async () => {
    api.updatePolicy.mockRejectedValue(new Error('500'));
    render(OrgPrivacyPolicyPanel);
    await screen.findByTestId('org-gender-switch');

    await fireEvent.click(sw('org-gender'));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(sw('org-gender').checked).toBe(true);
  });

  it('offers deletion only once the switch is off', async () => {
    render(OrgPrivacyPolicyPanel);
    await screen.findByTestId('org-voiceprints-switch');
    expect(purgeBtn('org-voiceprints')).toBeDisabled();
    expect(screen.getByTestId('org-voiceprints-purge')).toHaveAccessibleDescription(
      'Turn the switch off first to delete the stored data.'
    );
  });

  it('deletes only after the typed confirmation, for the right target', async () => {
    api.getPolicy.mockResolvedValue(policy({ gender: false }));
    api.purgePolicyData.mockResolvedValue({ task_id: 't-9' });
    render(OrgPrivacyPolicyPanel);
    await screen.findByTestId('org-gender-switch');
    expect(purgeBtn('org-gender')).toBeEnabled();
    expect(purgeBtn('org-voiceprints')).toBeDisabled();

    await fireEvent.click(purgeBtn('org-gender'));
    const confirm = screen.getByTestId('purge-confirm');
    expect(confirm).toBeDisabled();
    expect(api.purgePolicyData).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('checkbox', { name: /I understand/ }));
    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'PURGE' } });
    await fireEvent.click(confirm);

    await waitFor(() => expect(api.purgePolicyData).toHaveBeenCalledWith(['inferred_gender']));
  });

  it.each([403, 404])(
    'explains an unavailable policy on a %s instead of an empty panel',
    async (status) => {
      api.getPolicy.mockRejectedValue({ response: { status } });
      render(OrgPrivacyPolicyPanel);
      expect(await screen.findByText('Not available')).toBeInTheDocument();
      expect(screen.queryByTestId('org-voiceprints-switch')).toBeNull();
    }
  );

  it('offers a retry after a server error', async () => {
    api.getPolicy.mockRejectedValueOnce({ response: { status: 500 } });
    render(OrgPrivacyPolicyPanel);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load');
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByTestId('org-voiceprints-switch')).toBeInTheDocument();
  });
});
