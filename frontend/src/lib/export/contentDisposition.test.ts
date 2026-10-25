import { describe, it, expect } from 'vitest';
import { parseContentDispositionFilename } from './contentDisposition';

describe('parseContentDispositionFilename (issue #1283)', () => {
  it('prefers the exact filename* form over the ASCII fallback', () => {
    const header =
      'attachment; filename="download.txt"; filename*=UTF-8\'\'%D8%A7%D8%AC%D8%AA%D9%85%D8%A7%D8%B9.txt';
    expect(parseContentDispositionFilename(header)).toBe('اجتماع.txt');
  });

  it('decodes CJK and accented names', () => {
    expect(
      parseContentDispositionFilename(
        'attachment; filename="download.md"; filename*=UTF-8\'\'%E4%BC%9A%E8%AE%AE-summary.md'
      )
    ).toBe('会议-summary.md');
    expect(
      parseContentDispositionFilename(
        'attachment; filename="Reunion cafe.srt"; filename*=UTF-8\'\'R%C3%A9union%20caf%C3%A9.srt'
      )
    ).toBe('Réunion café.srt');
  });

  it('falls back to the quoted ASCII filename', () => {
    expect(parseContentDispositionFilename('attachment; filename="meeting.txt"')).toBe(
      'meeting.txt'
    );
  });

  it('falls back to the ASCII form when the escape in filename* is malformed', () => {
    expect(
      parseContentDispositionFilename('attachment; filename="a.txt"; filename*=UTF-8\'\'%E4%BC%')
    ).toBe('a.txt');
  });

  it('accepts an unquoted filename', () => {
    expect(parseContentDispositionFilename('attachment; filename=audit-logs.csv')).toBe(
      'audit-logs.csv'
    );
  });

  it.each([undefined, null, '', 'attachment', 42])('returns null for %s', (header) => {
    expect(parseContentDispositionFilename(header)).toBeNull();
  });
});
