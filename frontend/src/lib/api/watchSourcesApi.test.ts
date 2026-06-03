import { describe, it, expect } from 'vitest';
import { getSourceTypeLabel } from './watchSourcesApi';

describe('watchSourcesApi', () => {
  it('maps source types to human labels', () => {
    expect(getSourceTypeLabel('local')).toBe('Local Folder');
    expect(getSourceTypeLabel('s3')).toBe('S3 Bucket');
    expect(getSourceTypeLabel('smb')).toBe('SMB Share');
  });

  it('falls back to the raw type for unknown values', () => {
    // @ts-expect-error testing the fallback path with an out-of-union value
    expect(getSourceTypeLabel('ftp')).toBe('ftp');
  });
});
