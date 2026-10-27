/**
 * A person's own voice privacy (issue #1280). The voiceprint switch is the user layer: when a
 * layer above has it off it is disabled and says who. Deleting inferred gender is offered only
 * once the gender switch (which lives in Voice Attributes) is off.
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
  getMine: vi.fn(),
  setMine: vi.fn(),
  purgeMine: vi.fn(),
  getPurgeStatus: vi.fn(),
}));
vi.mock('$lib/api/privacy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/privacy')>();
  return { ...actual, PrivacyApi: api };
});
const attrs = vi.hoisted(() => ({ getSpeakerAttributeSettings: vi.fn() }));
vi.mock('$lib/api/speakerAttributeSettings', () => attrs);
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import UserPrivacyCard from './UserPrivacyCard.svelte';

const sw = () => screen.getByTestId('user-voiceprints-switch') as HTMLInputElement;
const genderPurge = () => screen.getByTestId('user-gender-purge') as HTMLButtonElement;
const settings = (gender: boolean) => ({
  detection_enabled: true,
  gender_detection_enabled: gender,
  show_attributes_on_cards: true,
  auto_llm_speaker_identification: true,
});

beforeEach(() => {
  vi.clearAllMocks();
  api.getMine.mockResolvedValue({ voiceprint_matching_enabled: true, locked_by: null });
  api.getPurgeStatus.mockResolvedValue({ task_id: 't', status: 'completed', counts: {} });
  attrs.getSpeakerAttributeSettings.mockResolvedValue(settings(true));
});

describe('UserPrivacyCard', () => {
  it('shows the voiceprint switch on and enabled by default', async () => {
    render(UserPrivacyCard);
    await screen.findByTestId('user-voiceprints-switch');
    expect(sw().checked).toBe(true);
    expect(sw()).toBeEnabled();
    expect(screen.getByText('Recognize speakers across my recordings')).toBeInTheDocument();
  });

  it.each([
    ['organization', 'Turned off by your organization administrator'],
    ['instance', 'Turned off by the deployment operator'],
    ['platform', 'Turned off by the platform operator'],
  ] as const)('locks the switch when the %s has it off and says so', async (layer, sentence) => {
    api.getMine.mockResolvedValue({ voiceprint_matching_enabled: true, locked_by: layer });
    render(UserPrivacyCard);
    await screen.findByTestId('user-voiceprints-switch');
    expect(sw()).toBeDisabled();
    expect(screen.getByTestId('user-voiceprints-locked')).toHaveTextContent(sentence);
  });

  it('saves the switch and shows the server state', async () => {
    api.setMine.mockResolvedValue({ voiceprint_matching_enabled: false, locked_by: null });
    render(UserPrivacyCard);
    await screen.findByTestId('user-voiceprints-switch');
    await fireEvent.click(sw());
    await waitFor(() => expect(api.setMine).toHaveBeenCalledWith(false));
    await waitFor(() => expect(sw().checked).toBe(false));
  });

  it('re-reads the server when a save is refused (a layer above locked it meanwhile)', async () => {
    api.setMine.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: { code: 'policy_locked', locked_by: 'organization' } },
      },
    });
    render(UserPrivacyCard);
    await screen.findByTestId('user-voiceprints-switch');
    api.getMine.mockResolvedValue({ voiceprint_matching_enabled: true, locked_by: 'organization' });
    await fireEvent.click(sw());
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    await waitFor(() => expect(sw()).toBeDisabled());
  });

  it('offers deleting my voiceprints only once my switch is off', async () => {
    api.getMine.mockResolvedValue({ voiceprint_matching_enabled: false, locked_by: null });
    api.purgeMine.mockResolvedValue({ task_id: 't' });
    render(UserPrivacyCard);
    await screen.findByTestId('user-voiceprints-switch');
    const button = screen.getByTestId('user-voiceprints-purge');
    expect(button).toBeEnabled();

    await fireEvent.click(button);
    await fireEvent.click(screen.getByRole('checkbox', { name: /I understand/ }));
    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'PURGE' } });
    await fireEvent.click(screen.getByTestId('purge-confirm'));
    await waitFor(() => expect(api.purgeMine).toHaveBeenCalledWith(['voiceprints']));
  });

  it('keeps the voiceprint deletion disabled while the switch is on', async () => {
    render(UserPrivacyCard);
    await screen.findByTestId('user-voiceprints-switch');
    expect(screen.getByTestId('user-voiceprints-purge')).toBeDisabled();
  });

  it('keeps deleting inferred gender disabled while the gender switch is on, and enables it when off', async () => {
    const view = render(UserPrivacyCard);
    await screen.findByTestId('user-voiceprints-switch');
    expect(genderPurge()).toBeDisabled();

    attrs.getSpeakerAttributeSettings.mockResolvedValue(settings(false));
    await view.component.reload();
    await waitFor(() => expect(genderPurge()).toBeEnabled());
  });

  it('has no gender switch of its own (one switch, one place)', async () => {
    render(UserPrivacyCard);
    await screen.findByTestId('user-voiceprints-switch');
    expect(screen.queryByTestId('user-gender-switch')).toBeNull();
  });

  it('keeps the gender deletion disabled when the setting cannot be read', async () => {
    attrs.getSpeakerAttributeSettings.mockRejectedValue(new Error('500'));
    render(UserPrivacyCard);
    await screen.findByTestId('user-voiceprints-switch');
    expect(genderPurge()).toBeDisabled();
  });
});
