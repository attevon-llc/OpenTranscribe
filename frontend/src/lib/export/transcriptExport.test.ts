import { describe, it, expect } from 'vitest';
import { buildExportContent, type ExportStrings } from './transcriptExport';

/**
 * Golden tests that LOCK the transcript export serialization format. The expected
 * strings below are derived directly from the switch logic that was moved verbatim
 * out of the file-detail page, so any drift in output is a regression.
 */

// Resolved i18n strings, matching the en locale's fileDetail.* keys.
const translations: ExportStrings = {
  speakerDefault: 'Speaker',
  userComment: 'USER COMMENT',
  commentType: 'COMMENT',
  csvHeaderDefault: 'Start Time,End Time,Speaker,Text',
  csvHeaderWithComments: 'Start Time,End Time,Speaker,Text,Comment Type',
};

// 3 segments: two consecutive from the same raw speaker (so TXT groups them via the
// SPEAKER_00 -> Alice mapping), then one with NO speaker info so it exercises the
// `speakerDefault` ('Speaker') fallback. Quote in the third tests CSV escaping.
const transcriptData = [
  { start_time: 0, end_time: 2, speaker_label: 'SPEAKER_00', text: 'Hello there.' },
  { start_time: 2, end_time: 4, speaker_label: 'SPEAKER_00', text: 'How are you?' },
  { start_time: 5, end_time: 7, text: 'I am fine, "thanks".' },
];

// Maps SPEAKER_00 -> Alice. The third segment has no label, so it resolves to the
// `speakerDefault` translation.
const speakerList = [{ name: 'SPEAKER_00', display_name: 'Alice' }];

// One comment between the two speaker groups (timestamp 3 -> "00:03").
const fileComments = [
  {
    timestamp: 3,
    text: 'Nice point!',
    user: { full_name: 'Bob Jones' },
  },
];

const baseOpts = {
  filename: 'meeting',
  jsonMeta: { filename: 'meeting.mp3', duration: 7 },
  translations,
};

describe('buildExportContent — txt', () => {
  it('groups consecutive same-speaker segments, with timestamps + speakers, no comments', () => {
    const out = buildExportContent('txt', transcriptData, speakerList, [], {
      ...baseOpts,
      includeComments: false,
      txtOptions: { includeTimestamps: true, includeSpeakers: true },
    });
    expect(out).toBe(
      '[00:00 --> 00:04] Alice:\nHello there. How are you?\n\n' +
        '[00:05 --> 00:07] Speaker:\nI am fine, "thanks".'
    );
  });

  it('omits timestamps and speakers when both toggles are off', () => {
    const out = buildExportContent('txt', transcriptData, speakerList, [], {
      ...baseOpts,
      includeComments: false,
      txtOptions: { includeTimestamps: false, includeSpeakers: false },
    });
    expect(out).toBe('Hello there. How are you?\n\nI am fine, "thanks".');
  });

  it('appends the comment after the grouped segments (preserved merge quirk)', () => {
    // NOTE: this locks a pre-existing behavior. mergeCommentsWithTranscript zips the
    // GROUPED `segments` array (2 entries) against the per-raw-segment `segmentTimes`
    // ([0, 2, 5]); the comment at t=3 only compares against segmentTimes[0..1] and so
    // lands AFTER both groups rather than strictly between them. Faithfully preserved.
    const out = buildExportContent('txt', transcriptData, speakerList, fileComments, {
      ...baseOpts,
      includeComments: true,
      txtOptions: { includeTimestamps: true, includeSpeakers: true },
    });
    expect(out).toBe(
      '[00:00 --> 00:04] Alice:\nHello there. How are you?\n\n' +
        '[00:05 --> 00:07] Speaker:\nI am fine, "thanks".\n\n' +
        '[00:03] USER COMMENT: Bob Jones: Nice point!'
    );
  });
});

describe('buildExportContent — json', () => {
  it('serializes segments using jsonMeta filename/duration, no comments', () => {
    const out = buildExportContent('json', transcriptData, speakerList, [], {
      ...baseOpts,
      includeComments: false,
    });
    expect(JSON.parse(out)).toEqual({
      filename: 'meeting.mp3',
      duration: 7,
      segments: [
        { start_time: 0, end_time: 2, speaker: 'Alice', text: 'Hello there.' },
        { start_time: 2, end_time: 4, speaker: 'Alice', text: 'How are you?' },
        { start_time: 5, end_time: 7, speaker: 'Speaker', text: 'I am fine, "thanks".' },
      ],
    });
  });

  it('appends a comments array when comments are included', () => {
    const out = buildExportContent('json', transcriptData, speakerList, fileComments, {
      ...baseOpts,
      includeComments: true,
    });
    expect(JSON.parse(out)).toEqual({
      filename: 'meeting.mp3',
      duration: 7,
      segments: [
        { start_time: 0, end_time: 2, speaker: 'Alice', text: 'Hello there.' },
        { start_time: 2, end_time: 4, speaker: 'Alice', text: 'How are you?' },
        { start_time: 5, end_time: 7, speaker: 'Speaker', text: 'I am fine, "thanks".' },
      ],
      comments: [{ timestamp: 3, user: 'Bob Jones', text: 'Nice point!' }],
    });
  });

  it('produces 2-space-indented JSON (exact)', () => {
    const out = buildExportContent('json', transcriptData, speakerList, [], {
      ...baseOpts,
      includeComments: false,
    });
    expect(out).toBe(
      JSON.stringify(
        {
          filename: 'meeting.mp3',
          duration: 7,
          segments: [
            { start_time: 0, end_time: 2, speaker: 'Alice', text: 'Hello there.' },
            { start_time: 2, end_time: 4, speaker: 'Alice', text: 'How are you?' },
            { start_time: 5, end_time: 7, speaker: 'Speaker', text: 'I am fine, "thanks".' },
          ],
        },
        null,
        2
      )
    );
  });
});

describe('buildExportContent — csv', () => {
  it('emits the default header and quoted/escaped rows, no comments', () => {
    const out = buildExportContent('csv', transcriptData, speakerList, [], {
      ...baseOpts,
      includeComments: false,
    });
    expect(out).toBe(
      'Start Time,End Time,Speaker,Text\n' +
        '0,2,"Alice","Hello there."\n' +
        '2,4,"Alice","How are you?"\n' +
        '5,7,"Speaker","I am fine, ""thanks""."'
    );
  });

  it('switches to the with-comments header and merges comment rows chronologically', () => {
    const out = buildExportContent('csv', transcriptData, speakerList, fileComments, {
      ...baseOpts,
      includeComments: true,
    });
    // The last segment row is the escaped text `"I am fine, ""thanks""."` followed by
    // the appended empty Comment Type column `,""`.
    expect(out).toBe(
      'Start Time,End Time,Speaker,Text,Comment Type\n' +
        '0,2,"Alice","Hello there.",""\n' +
        '2,4,"Alice","How are you?",""\n' +
        '3,3,"USER COMMENT: Bob Jones","Nice point!","COMMENT"\n' +
        '5,7,"Speaker","I am fine, ""thanks"".",""'
    );
  });
});

describe('buildExportContent — srt', () => {
  it('numbers cues and formats SRT timestamps, no comments', () => {
    const out = buildExportContent('srt', transcriptData, speakerList, [], {
      ...baseOpts,
      includeComments: false,
    });
    expect(out).toBe(
      '1\n00:00:00,000 --> 00:00:02,000\nAlice: Hello there.\n\n' +
        '2\n00:00:02,000 --> 00:00:04,000\nAlice: How are you?\n\n' +
        '3\n00:00:05,000 --> 00:00:07,000\nSpeaker: I am fine, "thanks".\n'
    );
  });

  it('inserts a comment cue (2s window) and re-numbers after sorting', () => {
    const out = buildExportContent('srt', transcriptData, speakerList, fileComments, {
      ...baseOpts,
      includeComments: true,
    });
    expect(out).toBe(
      '1\n00:00:00,000 --> 00:00:02,000\nAlice: Hello there.\n\n' +
        '2\n00:00:02,000 --> 00:00:04,000\nAlice: How are you?\n\n' +
        '3\n00:00:03,000 --> 00:00:05,000\nUSER COMMENT: Bob Jones: Nice point!\n\n' +
        '4\n00:00:05,000 --> 00:00:07,000\nSpeaker: I am fine, "thanks".\n'
    );
  });
});

describe('buildExportContent — vtt', () => {
  it('emits the WEBVTT header and dot-separated cues, no comments', () => {
    const out = buildExportContent('vtt', transcriptData, speakerList, [], {
      ...baseOpts,
      includeComments: false,
    });
    expect(out).toBe(
      'WEBVTT\n\n' +
        '00:00:00.000 --> 00:00:02.000\nAlice: Hello there.\n\n' +
        '00:00:02.000 --> 00:00:04.000\nAlice: How are you?\n\n' +
        '00:00:05.000 --> 00:00:07.000\nSpeaker: I am fine, "thanks".\n'
    );
  });

  it('inserts a comment cue (2s window) sorted by start time', () => {
    const out = buildExportContent('vtt', transcriptData, speakerList, fileComments, {
      ...baseOpts,
      includeComments: true,
    });
    expect(out).toBe(
      'WEBVTT\n\n' +
        '00:00:00.000 --> 00:00:02.000\nAlice: Hello there.\n\n' +
        '00:00:02.000 --> 00:00:04.000\nAlice: How are you?\n\n' +
        '00:00:03.000 --> 00:00:05.000\nUSER COMMENT: Bob Jones: Nice point!\n\n' +
        '00:00:05.000 --> 00:00:07.000\nSpeaker: I am fine, "thanks".\n'
    );
  });
});
