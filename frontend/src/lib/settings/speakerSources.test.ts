import { describe, it, expect } from 'vitest';
import en from '$lib/i18n/locales/en.json';
import {
  selectionNotices,
  selectionWhere,
  speakerSourceOptions,
  transcriptionFacts,
} from './speakerSources';
import { fullLocalView, liteCloudView, twoVendorView } from './pipelineViewFixtures';

const EN_KEYS = new Set(Object.keys(en));
const hasKey = (key: string) => EN_KEYS.has(key);

describe('speakerSourceOptions', () => {
  it('offers the four sources on a full deployment, plus another provider when allowed', () => {
    expect(
      speakerSourceOptions({ mode: 'full', current: 'provider', asrConfigEnabled: false }).map(
        (o) => o.value
      )
    ).toEqual(['provider', 'local', 'pyannote', 'off']);
    expect(
      speakerSourceOptions({ mode: 'full', current: 'provider', asrConfigEnabled: true }).map(
        (o) => o.value
      )
    ).toEqual(['provider', 'local', 'pyannote', 'asr_config', 'off']);
  });

  it('does not render the local option at all on lite', () => {
    const values = speakerSourceOptions({
      mode: 'lite',
      current: 'provider',
      asrConfigEnabled: true,
    }).map((o) => o.value);
    expect(values).not.toContain('local');
    expect(values).toEqual(['provider', 'pyannote', 'asr_config', 'off']);
  });

  it('keeps a stored local selection visible on lite, disabled, so the select can show it', () => {
    const local = speakerSourceOptions({
      mode: 'lite',
      current: 'local',
      asrConfigEnabled: false,
    }).find((o) => o.value === 'local');
    expect(local?.disabled).toBe(true);
    expect(local?.labelKey).toBe('settings.speakerIdentification.source.localUnavailableOption');
  });

  it('keeps a stored asr_config selection visible when the capability is off', () => {
    expect(
      speakerSourceOptions({ mode: 'full', current: 'asr_config', asrConfigEnabled: false }).map(
        (o) => o.value
      )
    ).toContain('asr_config');
  });

  it('only references copy that exists in en.json', () => {
    for (const mode of ['full', 'lite'] as const) {
      for (const current of ['provider', 'local', 'pyannote', 'asr_config', 'off'] as const) {
        for (const option of speakerSourceOptions({ mode, current, asrConfigEnabled: true })) {
          expect(hasKey(option.labelKey), option.labelKey).toBe(true);
          expect(hasKey(option.descKey), option.descKey).toBe(true);
        }
      }
    }
  });
});

describe('transcriptionFacts', () => {
  it('reads speakers and timing off the transcription step', () => {
    expect(transcriptionFacts(twoVendorView())).toEqual({
      returnsSpeakers: false,
      timing: 'segment',
    });
  });

  it('is null while the pipeline view has not loaded', () => {
    expect(transcriptionFacts(null)).toBeNull();
  });
});

describe('selectionNotices', () => {
  const textOnlyTimed = { returnsSpeakers: false, timing: 'segment' as const };
  const textOnlyNoTiming = { returnsSpeakers: false, timing: 'none' as const };
  const speakersWord = { returnsSpeakers: true, timing: 'word' as const };
  const codes = (...args: Parameters<typeof selectionNotices>) =>
    selectionNotices(...args).map((n) => n.code);

  it('is quiet when the transcription model returns speakers with word timing', () => {
    expect(codes('provider', speakersWord, 'full')).toEqual([]);
    expect(codes('local', speakersWord, 'full')).toEqual([]);
  });

  it('warns that Automatic with a text-only model detects speakers on this server (#1219)', () => {
    expect(codes('provider', textOnlyTimed, 'full')).toEqual([
      'automatic_uses_server',
      'segment_timing_only',
    ]);
  });

  it('refuses Automatic with a text-only model on lite and steers to another provider', () => {
    const notices = selectionNotices('provider', textOnlyTimed, 'lite');
    expect(notices[0]).toEqual({ code: 'automatic_needs_speaker_provider', severity: 'error' });
  });

  it('refuses any speaker detection when the model returns no timestamps', () => {
    for (const source of ['provider', 'local', 'pyannote', 'asr_config'] as const) {
      expect(selectionNotices(source, textOnlyNoTiming, 'full')[0]).toEqual({
        code: 'asr_no_timing_for_speakers',
        severity: 'error',
      });
    }
  });

  it('allows a no-timing model when speaker detection is off', () => {
    expect(codes('off', textOnlyNoTiming, 'full')).toEqual([]);
  });

  it('refuses the local source on lite', () => {
    expect(codes('local', speakersWord, 'lite')).toEqual(['local_diarization_unavailable']);
  });

  it('warns about segment timing for a cloud diarizer too, but not about Automatic', () => {
    expect(codes('pyannote', textOnlyTimed, 'full')).toEqual(['segment_timing_only']);
  });

  it('does not warn about segment timing when the model supplies its own speaker labels', () => {
    const labelsAtSegmentLevel = { returnsSpeakers: true, timing: 'segment' as const };
    expect(codes('provider', labelsAtSegmentLevel, 'full')).toEqual([]);
    // ...but a speaker detector chosen separately still has to be merged into that text.
    expect(codes('pyannote', labelsAtSegmentLevel, 'full')).toEqual(['segment_timing_only']);
  });

  it('is quiet before the transcription model is known', () => {
    expect(codes('provider', null, 'full')).toEqual([]);
    expect(codes('local', null, 'lite')).toEqual(['local_diarization_unavailable']);
  });
});

describe('selectionWhere', () => {
  const choice = { uuid: 'u', name: 'n', providerLabel: 'Deepgram', modelName: 'm', owner: null };

  it('says this server for the local source and nothing for off', () => {
    expect(selectionWhere('local', fullLocalView(), undefined)).toEqual({
      where: 'server',
      vendor: null,
    });
    expect(selectionWhere('off', fullLocalView(), undefined)).toBeNull();
  });

  it('names pyannote.ai and the chosen provider for the cloud sources', () => {
    expect(selectionWhere('pyannote', fullLocalView(), undefined)).toEqual({
      where: 'cloud',
      vendor: 'pyannote.ai',
    });
    expect(selectionWhere('asr_config', fullLocalView(), choice)).toEqual({
      where: 'cloud',
      vendor: 'Deepgram',
    });
    expect(selectionWhere('asr_config', fullLocalView(), undefined)).toBeNull();
  });

  it('follows the transcription provider for Automatic when it returns speakers', () => {
    expect(selectionWhere('provider', liteCloudView(), undefined)).toEqual({
      where: 'cloud',
      vendor: 'Deepgram',
    });
    expect(selectionWhere('provider', fullLocalView(), undefined)).toEqual({
      where: 'server',
      vendor: null,
    });
  });

  it('falls back to this server for Automatic with a text-only model, and to nothing on lite', () => {
    expect(selectionWhere('provider', twoVendorView(), undefined)).toEqual({
      where: 'server',
      vendor: null,
    });
    const lite = twoVendorView();
    lite.deployment_mode = 'lite';
    expect(selectionWhere('provider', lite, undefined)).toBeNull();
  });
});
