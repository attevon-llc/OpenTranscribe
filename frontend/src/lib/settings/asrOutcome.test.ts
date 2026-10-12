import { describe, it, expect } from 'vitest';
import en from '$lib/i18n/locales/en.json';
import { changeMessages, notAutoActivatedMessages } from './asrOutcome';
import { issue } from './pipelineViewFixtures';

const KEYS = new Set(Object.keys(en));

describe('changeMessages (after deleting, activating or clearing an ASR config)', () => {
  it('says speaker detection went back to Automatic when the deleted config was its source', () => {
    expect(changeMessages({ diarizer_source_reverted: true, pipeline_issues: [] })).toEqual([
      { severity: 'info', key: 'settings.asrProvider.diarizerReverted', fallbackText: null },
    ]);
  });

  it('names each blocking or advisory problem by its code, errors first', () => {
    const messages = changeMessages({
      pipeline_issues: [
        issue('segment_timing_only', 'warning'),
        issue('asr_no_timing_for_speakers', 'error'),
      ],
    });
    expect(messages.map((m) => m.key)).toEqual([
      'settings.pipeline.issue.asr_no_timing_for_speakers',
      'settings.pipeline.issue.segment_timing_only',
    ]);
    expect(messages.map((m) => m.severity)).toEqual(['error', 'warning']);
  });

  it('leaves out standing notes that no change of provider caused', () => {
    expect(
      changeMessages({
        pipeline_issues: [
          issue('speaker_hints_ignored', 'warning'),
          issue('source_locked', 'info'),
          issue('automatic_uses_server', 'info'),
        ],
      })
    ).toEqual([]);
  });

  it('uses the fixed server sentence only for a code it has no copy for', () => {
    expect(changeMessages({ pipeline_issues: [issue('brand_new_code', 'error')] })).toEqual([
      { severity: 'error', key: null, fallbackText: 'fixed sentence for brand_new_code' },
    ]);
  });

  it('is empty when nothing changed and nothing is wrong', () => {
    expect(changeMessages({})).toEqual([]);
    expect(changeMessages({ diarizer_source_reverted: false, pipeline_issues: [] })).toEqual([]);
  });

  it('only references copy that exists in en.json', () => {
    const messages = changeMessages({
      diarizer_source_reverted: true,
      pipeline_issues: [issue('pyannote_key_missing', 'error')],
    });
    for (const m of messages) expect(KEYS.has(m.key as string), String(m.key)).toBe(true);
  });
});

describe('notAutoActivatedMessages (after creating a config)', () => {
  it('tells the user a first config was saved but not made active, with why', () => {
    const messages = notAutoActivatedMessages(
      { auto_activated: false, pipeline_issues: [issue('asr_no_timing_for_speakers', 'error')] },
      true
    );
    expect(messages[0].key).toBe('settings.asrProvider.notAutoActivated');
    expect(messages.slice(1).map((m) => m.key)).toEqual([
      'settings.pipeline.issue.asr_no_timing_for_speakers',
    ]);
  });

  it('says nothing when it was activated', () => {
    expect(notAutoActivatedMessages({ auto_activated: true, pipeline_issues: [] }, true)).toEqual(
      []
    );
  });

  it('says nothing for a later config, which is never auto-activated by design', () => {
    expect(notAutoActivatedMessages({ auto_activated: false, pipeline_issues: [] }, false)).toEqual(
      []
    );
  });

  it('references copy that exists in en.json', () => {
    expect(KEYS.has('settings.asrProvider.notAutoActivated')).toBe(true);
    expect(KEYS.has('settings.asrProvider.diarizerReverted')).toBe(true);
  });
});
