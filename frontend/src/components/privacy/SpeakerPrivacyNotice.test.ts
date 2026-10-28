/**
 * The explanation on speaker surfaces (issue #1280). It must name the layer that switched voice
 * matching off, say gender is inferred (and link to the switch) only while inference is on, stay
 * silent when the policy cannot be read, and re-read for another file.
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

const api = vi.hoisted(() => ({ getEffective: vi.fn() }));
vi.mock('$lib/api/privacy', () => ({ PrivacyApi: api }));

import SpeakerPrivacyNotice from './SpeakerPrivacyNotice.svelte';
import { resetEffectivePrivacy } from '$stores/privacyPolicy';
import { settingsModalStore } from '$stores/settingsModalStore';
import { get } from 'svelte/store';

const effective = (
  voice: { enabled: boolean; locked_by: string | null },
  gender: { enabled: boolean; locked_by: string | null }
) => ({ organization_uuid: null, voiceprint_matching: voice, gender_inference: gender });
const ON = { enabled: true, locked_by: null };
const off = (layer: string) => ({ enabled: false, locked_by: layer });

beforeEach(() => {
  vi.clearAllMocks();
  resetEffectivePrivacy();
  settingsModalStore.reset();
});

describe('SpeakerPrivacyNotice', () => {
  it('says speaker recognition is off and who turned it off', async () => {
    api.getEffective.mockResolvedValue(effective(off('organization'), ON));
    render(SpeakerPrivacyNotice, { show: 'voiceprints' });
    const note = await screen.findByTestId('voiceprints-off-notice');
    expect(note).toHaveTextContent('Speaker recognition is turned off.');
    expect(note).toHaveTextContent('Turned off by your organization administrator');
    expect(screen.queryByTestId('gender-inferred-notice')).toBeNull();
  });

  it('links to the switch only when the person turned it off themselves', async () => {
    api.getEffective.mockResolvedValue(effective(off('user'), ON));
    render(SpeakerPrivacyNotice, { show: 'voiceprints' });
    await fireEvent.click(await screen.findByRole('button', { name: 'Open settings' }));
    expect(get(settingsModalStore).isOpen).toBe(true);
    expect(get(settingsModalStore).activeSection).toBe('speaker-attributes');
  });

  it('offers no settings link when a layer above the person turned it off', async () => {
    api.getEffective.mockResolvedValue(effective(off('instance'), ON));
    render(SpeakerPrivacyNotice, { show: 'voiceprints' });
    await screen.findByTestId('voiceprints-off-notice');
    expect(screen.queryByRole('button', { name: 'Open settings' })).toBeNull();
  });

  it('says gender is inferred automatically while inference is on, with a link to the switch', async () => {
    api.getEffective.mockResolvedValue(effective(ON, ON));
    render(SpeakerPrivacyNotice, { show: 'gender' });
    const note = await screen.findByTestId('gender-inferred-notice');
    expect(note).toHaveTextContent('inferred automatically from voice');
    await fireEvent.click(screen.getByRole('button', { name: 'Change in settings' }));
    expect(get(settingsModalStore).activeSection).toBe('speaker-attributes');
  });

  it('says nothing about gender once inference is off (nothing is shown to explain)', async () => {
    api.getEffective.mockResolvedValue(effective(ON, off('organization')));
    render(SpeakerPrivacyNotice, { show: 'both' });
    await waitFor(() => expect(api.getEffective).toHaveBeenCalled());
    expect(screen.queryByTestId('speaker-privacy-notice')).toBeNull();
  });

  it('renders nothing when everything is allowed and no gender statement was asked for', async () => {
    api.getEffective.mockResolvedValue(effective(ON, ON));
    render(SpeakerPrivacyNotice, { show: 'voiceprints' });
    await waitFor(() => expect(api.getEffective).toHaveBeenCalled());
    expect(screen.queryByTestId('speaker-privacy-notice')).toBeNull();
  });

  it('renders nothing when the policy cannot be read', async () => {
    api.getEffective.mockRejectedValue(new Error('501'));
    render(SpeakerPrivacyNotice);
    await waitFor(() => expect(api.getEffective).toHaveBeenCalled());
    expect(screen.queryByTestId('speaker-privacy-notice')).toBeNull();
  });

  it('asks for the file it is shown on, and shares one read per file', async () => {
    api.getEffective.mockResolvedValue(effective(off('platform'), ON));
    render(SpeakerPrivacyNotice, { fileUuid: 'file-1', show: 'voiceprints' });
    render(SpeakerPrivacyNotice, { fileUuid: 'file-1', show: 'voiceprints' });
    await screen.findAllByTestId('voiceprints-off-notice');
    expect(api.getEffective).toHaveBeenCalledTimes(1);
    expect(api.getEffective).toHaveBeenCalledWith('file-1');
  });
});
