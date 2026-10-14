/**
 * Guards the English source-string fixes from issue #1220. Every locale is translated FROM
 * `en.json`, so copy that only works in English (a value glued onto a sentence, an adjective
 * with no noun, developer vocabulary in user prose) is re-broken in all 11 other languages.
 * `check:i18n` enforces key parity only, so these rules need their own test.
 */
import { describe, it, expect } from 'vitest';
import en from './locales/en.json';
import de from './locales/de.json';

const EN = en as Record<string, string>;
const DE = de as Record<string, string>;

describe('English source copy (#1220)', () => {
  it('has no uploader value that is a fragment glued onto another string', () => {
    const offenders = Object.entries(EN)
      .filter(([k]) => k.startsWith('uploader.'))
      .filter(([, v]) => /^\s/.test(v) || /^[–—-]\s/.test(v))
      .map(([k]) => k);
    expect(offenders).toEqual([]);
  });

  it('has no uploader Suffix key left over from concatenated copy', () => {
    expect(
      Object.keys(EN).filter((k) => k.startsWith('uploader.') && k.endsWith('Suffix'))
    ).toEqual([]);
  });

  it('gives each upload model option one self-contained string', () => {
    expect(EN['uploader.highQualityOption']).toContain('{{model}}');
    expect(EN['uploader.fastProcessingOption']).toBeTruthy();
  });

  it('does not say the Fast model has no speaker detection, because it detects them afterwards', () => {
    for (const key of [
      'uploader.fastProcessingOption',
      'uploader.fastProcessingHint',
      'uploader.reviewSpeakersFast',
    ]) {
      expect(EN[key]).toMatch(/after transcription/);
      expect(EN[key]).not.toMatch(/no speaker|not available|does not detect/i);
    }
    // The German copy is a real translation of the same claim, not the English string.
    expect(DE['uploader.fastProcessingOption']).toMatch(/nach der Transkription/);
  });

  it('never interpolates a level name into a break-glass sentence', () => {
    expect(EN['supportAccess.breakGlass.confirmSummary']).toBeUndefined();
    for (const level of ['Read', 'Write']) {
      const text = EN[`supportAccess.breakGlass.confirmSummary${level}`];
      expect(text).toBeTruthy();
      expect(text).not.toContain('{{level}}');
    }
  });

  it('keeps the language-default value shapes in the template, not the value', () => {
    expect(EN['settings.transcription.languageDefaultsValue']).toContain('{{source}}');
    expect(EN['settings.transcription.languageDefaultsValue']).toContain('{{translate}}');
    expect(EN['settings.transcription.aiLanguageDefaultsValue']).toContain('{{language}}');
    expect(EN['settings.transcription.aiLanguageDefaultsValue']).not.toMatch(/English/);
  });

  it('does not name a status with a bare adjective', () => {
    expect(EN['common.orphaned'].split(/\s+/).length).toBeGreaterThan(1);
  });

  it('keeps developer vocabulary out of user-facing engine prose', () => {
    const prose = [
      'settings.engineSettings.intro.summary',
      'settings.engineSettings.intro.details',
      'settings.engineSettings.boundaryAcousticScope',
    ];
    for (const key of prose) {
      expect(EN[key]).not.toMatch(
        /ENGINE_DIARIZER_BACKEND|ENV VAR|DEFAULT|diar-native|fast path|GPU-split/
      );
    }
    expect(EN['settings.engineSettings.intro.overrideAdmin']).toContain('ENGINE_DIARIZER_BACKEND');
  });

  it('uses one vocabulary for quarantine', () => {
    for (const [key, value] of Object.entries(EN)) {
      if (!/quarantine/i.test(key)) continue;
      expect(value, key).not.toMatch(/read surface|taken down/i);
    }
  });

  it('keeps storage nouns for orphaned data distinct', () => {
    expect(EN['settings.dataIntegrity.orphans.filesLabel']).not.toBe('Files left in storage');
    expect(EN['settings.dataIntegrity.orphans.objectsLabel']).not.toBe('Objects');
  });

  it('has no ASCII-stripped "fur" in the German source', () => {
    const offenders = Object.entries(DE)
      .filter(([, v]) => /\bfur\b/.test(v))
      .map(([k]) => k);
    expect(offenders).toEqual([]);
  });
});

describe('English source copy left over from #1220 (#1222)', () => {
  // Namespaces whose strings were glued onto a sentence by the component. `uploader.` is
  // guarded above; these are the other two places the same defect lived.
  const NAMESPACES = ['about.legal.', 'settings.embeddingConsistency.'];
  const inNamespaces = Object.entries(EN).filter(([k]) => NAMESPACES.some((n) => k.startsWith(n)));

  it('scans the namespaces it claims to guard', () => {
    expect(inNamespaces.length).toBeGreaterThan(20);
  });

  it('has no Suffix key left over from concatenated copy', () => {
    expect(inNamespaces.map(([k]) => k).filter((k) => k.endsWith('Suffix'))).toEqual([]);
  });

  it('has no value that is a fragment (leading space, dash or closing punctuation)', () => {
    const offenders = inNamespaces
      .filter(([, v]) => /^\s/.test(v) || /^[–—-]\s/.test(v) || /^[.,;:!?)]/.test(v))
      .map(([k]) => k);
    expect(offenders).toEqual([]);
  });

  it('keeps the licence and NOTICE links inside one sentence each', () => {
    expect(EN['about.legal.licensedUnder']).toContain('{{license}}');
    expect(EN['about.legal.thirdParty']).toContain('{{notice}}');
  });

  it('gives the repair ETA its own sentence, not a tail glued after the percentage', () => {
    expect(EN['settings.embeddingConsistency.eta']).toContain('{{eta}}');
    expect(EN['settings.embeddingConsistency.eta']).toMatch(/^[A-Za-z]/);
  });

  it('carries the same placeholders in every locale for the rewritten keys', () => {
    const placeholders = (v: string) => (v.match(/\{\{\w+\}\}/g) ?? []).sort().join(',');
    for (const key of [
      'about.legal.licensedUnder',
      'about.legal.thirdParty',
      'settings.embeddingConsistency.eta',
    ]) {
      expect(placeholders(DE[key]), key).toBe(placeholders(EN[key]));
    }
  });
});

// Issue #1243: capability flags and the cost of another provider's speakers, in every locale.
const LOCALES = import.meta.glob('./locales/*.json', { eager: true, import: 'default' }) as Record<
  string,
  Record<string, string>
>;
const CAPABILITY_COPY = [
  'settings.asrProvider.optionVaries',
  'settings.asrProvider.implication.speakersIncluded',
  'settings.asrProvider.implication.speakersLocal',
  'settings.asrProvider.implication.textOnly',
  'settings.asrProvider.implication.textOnlyLite',
  'settings.asrProvider.legend.title',
  'settings.asrProvider.legend.speakersIncluded',
  'settings.asrProvider.legend.textOnly',
  'settings.asrProvider.legend.timingWord',
  'settings.asrProvider.legend.timingSegment',
  'settings.asrProvider.legend.timingNone',
  'uploader.cloudDefaultOption',
  'settings.speakerIdentification.source.asrConfigSaved',
  'settings.speakerIdentification.source.asrConfigTranscribing',
  'settings.speakerIdentification.credential.asrConfig',
  'settings.speakerIdentification.billing.included',
  'settings.pipeline.issue.diarizer_double_billing',
  'settings.pipeline.issue.diarizer_same_as_transcription',
];

describe('capability and billing copy (#1243)', () => {
  it('is translated in every locale, not copied from English', () => {
    for (const [file, messages] of Object.entries(LOCALES)) {
      if (file.endsWith('/en.json')) continue;
      for (const key of CAPABILITY_COPY) {
        expect(messages[key], `${file} ${key}`).toBeTruthy();
        expect(messages[key], `${file} ${key}`).not.toBe(EN[key]);
      }
    }
  });

  it('keeps the placeholders of the one-string labels in every locale', () => {
    for (const [file, messages] of Object.entries(LOCALES)) {
      expect(messages['settings.asrProvider.flagsLine'], file).toMatch(
        /\{\{speakers\}\}.*\{\{timing\}\}/
      );
      expect(messages['settings.asrProvider.optionWithFlags'], file).toMatch(
        /\{\{name\}\}.*\{\{flags\}\}/
      );
      expect(messages['uploader.cloudDefaultOption'], file).toMatch(/\{\{provider\}\}/);
      expect(messages['uploader.cloudDefaultOption'], file).toMatch(/\{\{model\}\}/);
    }
  });

  it('says what another provider costs and what it receives, and that nothing is billed twice for the same one', () => {
    expect(EN['settings.pipeline.issue.diarizer_double_billing']).toMatch(/full transcription/);
    expect(EN['settings.pipeline.issue.diarizer_double_billing']).toMatch(/audio/);
    expect(EN['settings.pipeline.issue.diarizer_same_as_transcription']).toMatch(/billed twice/);
    expect(EN['settings.speakerIdentification.source.asrConfigDesc']).toMatch(/audio/);
  });

  it('left no orphaned copy behind', () => {
    for (const messages of Object.values(LOCALES)) {
      expect(messages['settings.speakerIdentification.source.asrConfigNote']).toBeUndefined();
      expect(messages['uploader.highQuality']).toBeUndefined();
      expect(messages['uploader.fastProcessing']).toBeUndefined();
    }
  });
});
