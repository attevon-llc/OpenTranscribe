/**
 * Parity guard for `MediaFileStatus` (issue #301).
 *
 * The TS status union is a hand-maintained mirror of the backend `FileStatus` enum — there
 * is no codegen and no `openapi-typescript` in package.json, so nothing else detects drift.
 * It had fallen three values behind (`queued`, `downloading`, `quarantined`), which meant
 * TypeScript rejected literals the API legitimately returns.
 *
 * The expected list below is transcribed from `backend/app/core/enums.py`. When you add a
 * status, this test should be the thing that fails.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import type { MediaFile, MediaFileStatus } from './media';

const BACKEND_ENUM_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../backend/app/core/enums.py'
);

/** Every value of the backend FileStatus enum, in declaration order. */
const EXPECTED_STATUSES: MediaFileStatus[] = [
  'pending',
  'queued',
  'downloading',
  'processing',
  'completed',
  'error',
  'cancelling',
  'cancelled',
  'orphaned',
  'quarantined',
];

/** Parse the FileStatus values straight out of the backend enum source. */
function readBackendFileStatuses(): string[] {
  const source = readFileSync(BACKEND_ENUM_PATH, 'utf-8');
  const classStart = source.indexOf('class FileStatus(');
  expect(classStart, 'FileStatus class not found in enums.py').toBeGreaterThan(-1);

  // Stop at the next top-level class/def so we only read FileStatus members.
  const rest = source.slice(classStart);
  const nextTopLevel = rest.slice(1).search(/\n(class |def )/);
  const body = nextTopLevel === -1 ? rest : rest.slice(0, nextTopLevel + 1);

  return [...body.matchAll(/^\s{4}[A-Z_]+\s*=\s*"([a-z_]+)"/gm)].map((m) => m[1]);
}

describe('MediaFileStatus', () => {
  it('matches the backend FileStatus enum exactly', () => {
    expect(readBackendFileStatuses()).toEqual(EXPECTED_STATUSES);
  });

  it('accepts every backend status as a MediaFile.status value', () => {
    // Compile-time proof: this only type-checks if the union covers all 10.
    for (const status of EXPECTED_STATUSES) {
      const file: Pick<MediaFile, 'status'> = { status };
      expect(file.status).toBe(status);
    }
  });

  it('includes the three statuses that were missing before #301', () => {
    // queued/downloading are normal early-pipeline states; quarantined is the
    // DMCA/abuse takedown state tracked alongside MediaFile.is_quarantined.
    for (const status of ['queued', 'downloading', 'quarantined'] as MediaFileStatus[]) {
      expect(EXPECTED_STATUSES).toContain(status);
    }
  });
});
