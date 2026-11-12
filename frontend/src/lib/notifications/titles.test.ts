/**
 * Pins the realtime notification text: which i18n key titles each frame type, and the
 * client-composed DMCA takedown notice. A wrong key renders as a raw key path in every
 * locale; a dropped counter-notice contact removes the owner's only dispute surface.
 */
import { describe, it, expect } from 'vitest';
import type { Translate } from '$lib/i18n';
import en from '$lib/i18n/locales/en.json';
import { locale as appLocale } from '$stores/locale';
import { TITLE_KEYS, composeClientMessage, notificationTitle } from './titles';

const english = en as Record<string, string>;

const echo: Translate = (key, opts) => (opts ? `${key}:${JSON.stringify(opts)}` : key);

// Spot pins by type, not a copy of the table: the exhaustive check below is against the
// locale file, which is what actually fails when a key is wrong.
const PINNED_TITLES: [string, string][] = [
  ['transcription_status', 'notifications.transcriptionUpdate'],
  ['download_progress', 'notifications.downloadProgress'],
  ['file_takedown', 'notifications.fileTakedown'],
  ['erasure_notice', 'notifications.erasureNotice'],
  ['reindex_complete', 'notifications.searchReindexing'],
  ['migration_finalized', 'notifications.embeddingMigration'],
];

describe('notificationTitle', () => {
  it.each(PINNED_TITLES)('%s -> %s', (type, key) => {
    expect(notificationTitle(type, echo)).toBe(key);
  });

  it('titles every known type with a key that exists in the English locale', () => {
    const entries = Object.entries(TITLE_KEYS);
    expect(entries.length).toBeGreaterThan(30);
    const missing = entries.filter(([, key]) => !(key in english));
    expect(missing).toEqual([]);
    for (const [type, key] of entries) expect(notificationTitle(type, echo)).toBe(key);
  });

  it('falls back to the generic title for an unknown type', () => {
    expect(notificationTitle('post_processing_status', echo)).toBe('notifications.notification');
    expect(notificationTitle('no_such_type', echo)).toBe('notifications.notification');
    expect('notifications.notification' in english).toBe(true);
  });
});

describe('composeClientMessage', () => {
  it('returns undefined for every non-takedown type, so the backend message is used', () => {
    expect(composeClientMessage('transcription_status', { filename: 'a' }, echo)).toBeUndefined();
  });

  it('names the file and reason and the counter-notice contact on a takedown', () => {
    const msg = composeClientMessage(
      'file_takedown',
      {
        filename: 'talk.mp3',
        reason: 'dmca',
        abuse_contact_email: 'abuse@example.com',
        file_uuid: 'f-1',
      },
      echo
    );
    expect(msg).toBe(
      'notifications.fileTakedownMessage:{"filename":"talk.mp3","reason":"dmca"} ' +
        'notifications.fileTakedownCounterNotice:{"email":"abuse@example.com","fileId":"f-1"}'
    );
  });

  it('uses the no-email counter-notice copy when no contact is configured', () => {
    const msg = composeClientMessage('file_takedown', {}, echo);
    expect(msg).toBe(
      'notifications.fileTakedownMessage:{"filename":"","reason":""} ' +
        'notifications.fileTakedownCounterNoticeNoEmail:{"fileId":""}'
    );
  });

  it('composes the release notice from the filename alone', () => {
    expect(composeClientMessage('file_takedown_released', { filename: 'x.wav' }, echo)).toBe(
      'notifications.fileTakedownReleasedMessage:{"filename":"x.wav"}'
    );
  });

  it.each([
    ['requested', 'notifications.erasureRequestedMessage'],
    ['reminder', 'notifications.erasureReminderMessage'],
    ['cancelled', 'notifications.erasureCancelledMessage'],
    ['completed', 'notifications.erasureCompletedMessage'],
  ])('words a scheduled-erasure %s notice from its event', (event, key) => {
    const msg = composeClientMessage('erasure_notice', { event }, echo);
    expect(msg).toBe(`${key}:{"when":""}`);
    expect(key in english).toBe(true);
  });

  it('formats the run time in the app locale, not the runtime default', () => {
    const iso = '2026-11-30T13:04:00Z';
    const expected = new Date(iso).toLocaleString('de');
    expect(expected).not.toBe(new Date(iso).toLocaleString('en-US'));
    appLocale.set('de');
    try {
      const msg = composeClientMessage(
        'erasure_notice',
        { event: 'requested', scheduled_for: iso },
        echo
      );
      expect(msg).toBe(
        `notifications.erasureRequestedMessage:${JSON.stringify({ when: expected })}`
      );
    } finally {
      appLocale.set('en');
    }
  });

  it('leaves an erasure notice of an unknown event to the backend message', () => {
    expect(composeClientMessage('erasure_notice', { event: 'other' }, echo)).toBeUndefined();
  });
});

describe('a task refused by the LLM token limits (#790)', () => {
  it.each([
    ['user_day', 'notifications.llmBudgetExceededDay'],
    ['instance_month', 'notifications.llmBudgetExceededMonth'],
  ])('translates the %s refusal instead of the English sentence', (scope, key) => {
    const data = {
      error_code: 'llm_budget_exceeded',
      budget_scope: scope,
      message: 'You have reached your daily AI token limit.',
    };
    expect(composeClientMessage('summarization_status', data, echo)).toBe(key);
  });

  it('translates an unchecked limit and leaves every other frame alone', () => {
    expect(
      composeClientMessage(
        'topic_extraction_status',
        { error_code: 'llm_budget_unavailable' },
        echo
      )
    ).toBe('notifications.llmBudgetUnavailable');
    expect(
      composeClientMessage('summarization_status', { error_code: 'topic_extraction_failed' }, echo)
    ).toBeUndefined();
  });
});
