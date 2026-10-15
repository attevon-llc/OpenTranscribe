import { describe, it, expect } from 'vitest';
import en from '$lib/i18n/locales/en.json';
import { audioVendors, buildPipelineSteps } from './pipelineSteps';
import { fullLocalView, issue, liteCloudView, step, twoVendorView } from './pipelineViewFixtures';

const EN_KEYS = new Set(Object.keys(en));
const hasKey = (key: string) => EN_KEYS.has(key);

describe('buildPipelineSteps', () => {
  it('returns the four steps in processing order', () => {
    const { steps } = buildPipelineSteps(fullLocalView());
    expect(steps.map((s) => s.step)).toEqual(['transcription', 'diarization', 'speaker_id', 'llm']);
  });

  it('shows this-server steps with the server chip and no vendor', () => {
    const [transcription] = buildPipelineSteps(fullLocalView()).steps;
    expect(transcription.where).toBe('server');
    expect(transcription.vendor).toBeNull();
    expect(transcription.engineLabelKey).toBe('settings.pipeline.engine.local_gpu');
    expect(transcription.model).toBe('large-v3-turbo');
  });

  it('names the cloud vendor on a cloud step and prints the provider as the engine line', () => {
    const [transcription, diarization] = buildPipelineSteps(liteCloudView()).steps;
    expect(transcription.where).toBe('cloud');
    expect(transcription.vendor).toBe('Deepgram');
    expect(transcription.engineLabelKey).toBeNull();
    expect(transcription.engineText).toBe('Deepgram');
    expect(diarization.engineLabelKey).toBe('settings.pipeline.engine.asr');
  });

  it('reports the speakers and timing badges from the transcription step only', () => {
    const { steps } = buildPipelineSteps(twoVendorView());
    expect(steps[0].returnsSpeakers).toBe(false);
    expect(steps[0].timing).toBe('segment');
    expect(steps[1].returnsSpeakers).toBeNull();
    expect(steps[1].timing).toBeNull();
  });

  it('carries the adapter billing flag to the speaker detection step', () => {
    const { steps } = buildPipelineSteps(twoVendorView());
    expect(steps[1].adapterBilling).toBe(true);
    expect(steps[0].adapterBilling).toBe(false);
  });

  it('dims a step that does not run and gives it no chip', () => {
    const view = fullLocalView();
    view.steps[3] = step({ step: 'llm', enabled: false, where: null, engine: 'off' });
    const llm = buildPipelineSteps(view).steps[3];
    expect(llm.enabled).toBe(false);
    expect(llm.where).toBeNull();
    expect(llm.engineLabelKey).toBeNull();
    expect(llm.engineText).toBeNull();
  });

  it('says whether the speaker range is used only for the detection step that runs', () => {
    const view = fullLocalView();
    view.steps[1] = { ...view.steps[1], speaker_hints: ['min', 'max'] };
    const { steps } = buildPipelineSteps(view);
    expect(steps[1].hintsKey).toBe('settings.pipeline.hintsUsed');
    expect(steps[0].hintsKey).toBeNull();
    const ignored = buildPipelineSteps(fullLocalView()).steps[1];
    expect(ignored.hintsKey).toBe('settings.pipeline.hintsIgnored');
    const off = fullLocalView();
    off.steps[1] = step({ step: 'diarization', enabled: false, where: null, engine: 'off' });
    expect(buildPipelineSteps(off).steps[1].hintsKey).toBeNull();
  });

  it('attaches each issue to its step, errors first, with fixed copy keyed by code', () => {
    const view = fullLocalView({
      issues: [
        issue('segment_timing_only', 'warning', 'diarization'),
        issue('local_asr_unavailable', 'error', 'transcription'),
        issue('pyannote_key_missing', 'error', 'diarization'),
      ],
    });
    const { steps } = buildPipelineSteps(view);
    expect(steps[0].issues.map((i) => i.code)).toEqual(['local_asr_unavailable']);
    expect(steps[1].issues.map((i) => i.code)).toEqual([
      'pyannote_key_missing',
      'segment_timing_only',
    ]);
    expect(steps[1].issues[0].messageKey).toBe('settings.pipeline.issue.pyannote_key_missing');
    expect(steps[1].issues[0].fixSections).toEqual(['speaker-identification']);
  });

  it('never prints the server sentence for a known code, only for one it cannot translate', () => {
    const view = fullLocalView({
      issues: [issue('pyannote_key_missing', 'error'), issue('brand_new_code', 'warning')],
    });
    const [known, unknown] = buildPipelineSteps(view).steps[1].issues;
    expect(known.messageKey).toBe('settings.pipeline.issue.pyannote_key_missing');
    expect(known.fallbackText).toBeNull();
    expect(unknown.messageKey).toBeNull();
    expect(unknown.fallbackText).toBe('fixed sentence for brand_new_code');
  });

  it('shows the lite banner only on lite and marks a blocked local step with its issue', () => {
    expect(buildPipelineSteps(fullLocalView()).liteBanner).toBe(false);
    const lite = liteCloudView({
      issues: [issue('local_diarization_unavailable', 'error')],
    });
    const built = buildPipelineSteps(lite);
    expect(built.liteBanner).toBe(true);
    expect(built.steps[1].issues[0].severity).toBe('error');
  });

  it('exposes the deep link target of every step', () => {
    const { steps } = buildPipelineSteps(fullLocalView());
    expect(steps.map((s) => s.settingsSection)).toEqual([
      'asr-provider',
      'speaker-identification',
      'speaker-identification',
      'llm-provider',
    ]);
  });

  it('only references copy that exists in en.json', () => {
    const views = [
      fullLocalView(),
      liteCloudView(),
      twoVendorView(),
      fullLocalView({
        issues: [...(['asr_no_timing_for_speakers', 'source_locked'] as const)].map((c) =>
          issue(c, 'error')
        ),
      }),
    ];
    const keys = new Set<string>();
    for (const view of views) {
      const built = buildPipelineSteps(view);
      if (built.liteBanner) keys.add('settings.pipeline.liteBanner');
      for (const s of built.steps) {
        keys.add(s.titleKey);
        keys.add(s.purposeKey);
        if (s.engineLabelKey) keys.add(s.engineLabelKey);
        if (s.hintsKey) keys.add(s.hintsKey);
        for (const i of s.issues) if (i.messageKey) keys.add(i.messageKey);
      }
    }
    for (const key of keys) expect(hasKey(key), key).toBe(true);
  });
});

describe('audioVendors', () => {
  it('is empty when nothing leaves this server', () => {
    expect(audioVendors(fullLocalView())).toEqual([]);
  });

  it('names the one vendor that receives audio, once, although two steps use it', () => {
    expect(audioVendors(liteCloudView())).toEqual(['Deepgram']);
  });

  it('names both vendors when transcription and speaker detection use different ones', () => {
    expect(audioVendors(twoVendorView())).toEqual(['OpenAI', 'Deepgram']);
  });
});
