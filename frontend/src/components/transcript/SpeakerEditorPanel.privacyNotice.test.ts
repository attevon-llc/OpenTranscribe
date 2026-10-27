/**
 * The speaker editor explains a missing voice feature instead of looking mysteriously empty
 * (issue #1280). The panel must mount `SpeakerPrivacyNotice` for THIS file (the policy is the
 * file owner's and organization's), say voiceprints are off exactly when the effective policy
 * says so, and talk about gender only when a speaker row actually shows an inferred gender.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';

const api = vi.hoisted(() => ({ getEffective: vi.fn() }));
vi.mock('$lib/api/privacy', () => ({ PrivacyApi: api }));

import SpeakerEditorPanel from './SpeakerEditorPanel.svelte';
import { loadEffectivePrivacy, resetEffectivePrivacy } from '$stores/privacyPolicy';

const ON = { enabled: true, locked_by: null };
const effective = (voiceOn: boolean, genderOn: boolean) => ({
  organization_uuid: null,
  voiceprint_matching: voiceOn ? ON : { enabled: false, locked_by: 'organization' },
  gender_inference: genderOn ? ON : { enabled: false, locked_by: 'organization' },
});

const speaker = (over: Record<string, unknown> = {}) => ({
  uuid: 's1',
  name: 'SPEAKER_00',
  display_name: '',
  segment_timestamps: [],
  segment_count: 0,
  ...over,
});

function mountPanel(speakers: unknown[], fileUuid = 'file-77') {
  return render(SpeakerEditorPanel, { props: { file: { uuid: fileUuid }, speakerList: speakers } });
}

/** Wait until the notice has received (and rendered) the answer, so an absence check is meaningful. */
async function settled(fileUuid = 'file-77') {
  await loadEffectivePrivacy(fileUuid);
  await tick();
}

beforeEach(() => {
  vi.clearAllMocks();
  resetEffectivePrivacy();
});

describe('SpeakerEditorPanel privacy notice', () => {
  it('says speaker recognition is off when the effective policy has voiceprints off', async () => {
    api.getEffective.mockResolvedValue(effective(false, true));
    mountPanel([speaker()]);
    expect(await screen.findByTestId('voiceprints-off-notice')).toBeInTheDocument();
  });

  it('shows no notice at all when voiceprints are allowed and no gender is shown', async () => {
    api.getEffective.mockResolvedValue(effective(true, true));
    mountPanel([speaker()]);
    await settled();
    expect(screen.queryByTestId('speaker-privacy-notice')).toBeNull();
  });

  it('asks about the file being edited, not about the signed-in user', async () => {
    api.getEffective.mockResolvedValue(effective(false, true));
    mountPanel([speaker()], 'file-77');
    await screen.findByTestId('voiceprints-off-notice');
    expect(api.getEffective).toHaveBeenCalledWith('file-77');
  });

  it('explains inferred gender only while a speaker row shows an inferred gender', async () => {
    api.getEffective.mockResolvedValue(effective(true, true));
    mountPanel([speaker({ predicted_gender: 'female' })]);
    expect(await screen.findByTestId('gender-inferred-notice')).toBeInTheDocument();
  });

  it('stays silent about gender when the only gender is "unknown"', async () => {
    api.getEffective.mockResolvedValue(effective(true, true));
    mountPanel([speaker({ predicted_gender: 'unknown' })]);
    await settled();
    expect(screen.queryByTestId('gender-inferred-notice')).toBeNull();
  });
});
