import { describe, it, expect } from 'vitest';
import { exportErrorKey } from './exportErrors';
import { ExportJobError } from './exportJobStream';

const http = (status: number) => ({ response: { status } });

describe('exportErrorKey', () => {
  it('names the pending redaction scan for a 409', () => {
    expect(exportErrorKey(http(409))).toBe('transcript.exportRedactionPending');
  });

  it('names the unresolvable policy for a 503', () => {
    expect(exportErrorKey(http(503))).toBe('transcript.exportPolicyUnavailable');
  });

  it.each([
    ['redaction_pending', 'documentExport.errorRedactionPending'],
    ['too_long', 'documentExport.errorTooLong'],
    ['unavailable', 'documentExport.errorUnavailable'],
    ['fonts_unavailable', 'documentExport.errorFontsUnavailable'],
    ['failed', 'documentExport.errorFailed'],
  ] as const)('maps the job reason %s', (reason, key) => {
    expect(exportErrorKey(new ExportJobError(reason, 'x'))).toBe(key);
  });

  it('falls back to the generic failure for anything else', () => {
    expect(exportErrorKey(http(500))).toBe('transcript.exportFailed');
    expect(exportErrorKey(new ExportJobError(null, 'x'))).toBe('transcript.exportFailed');
    expect(exportErrorKey(new Error('network'))).toBe('transcript.exportFailed');
    expect(exportErrorKey(undefined)).toBe('transcript.exportFailed');
  });
});
