/**
 * Parity guard for the MediaFile error-reporting fields (issue #841).
 *
 * Modelled on `media.status.test.ts`: the TS mirrors under `$lib/types` are hand-maintained,
 * with no codegen and nothing else that detects drift. #841 shipped a backend `user_message`
 * field with no frontend consumer, and a dead `last_error_message`/`error_message` pair that
 * had been renamed/removed on one side and not the other. This test reads BOTH sides —
 * `backend/app/schemas/media.py`'s `class MediaFile` "Error handling fields" block, and the
 * TS `MediaFile`/`MediaFileDetail` interfaces — and fails if they ever diverge again.
 *
 * Deliberately scoped to the error-group fields only: a whole-schema TS/Pydantic mirror check
 * is the codegen project, out of scope here.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const BACKEND_SCHEMA_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../backend/app/schemas/media.py'
);
const TS_TYPES_PATH = resolve(dirname(fileURLToPath(import.meta.url)), './media.ts');

/** The exact fields the plan pins for this group. Kept explicit (not regex-derived on the
 * backend side) because `is_retryable` doesn't contain "error" or "message" at all — a
 * name-substring filter would silently drop it.
 */
const EXPECTED_ERROR_GROUP_FIELDS = [
  'error_reason',
  'error_suggestions',
  'user_message',
  'is_retryable',
];

/** Parse the "# Error handling fields" block inside `class MediaFile(...)` in the backend schema. */
function readBackendErrorGroupFields(): string[] {
  const source = readFileSync(BACKEND_SCHEMA_PATH, 'utf-8');
  const classStart = source.indexOf('class MediaFile(');
  expect(classStart, 'class MediaFile(...) not found in schemas/media.py').toBeGreaterThan(-1);

  const markerStart = source.indexOf('# Error handling fields', classStart);
  expect(
    markerStart,
    '"# Error handling fields" comment not found under class MediaFile'
  ).toBeGreaterThan(-1);

  const afterMarker = source.slice(markerStart);
  const lines = afterMarker.split('\n').slice(1); // skip the marker comment line itself

  const fields: string[] = [];
  let sawField = false;
  for (const line of lines) {
    const match = line.match(/^\s{4}([a-z_]+):/);
    if (match) {
      sawField = true;
      fields.push(match[1]);
      continue;
    }
    if (sawField) break; // the block ends at the first non-field line after fields began
    if (/^\s*#/.test(line) || line.trim() === '') continue; // leading explanatory comments
    break; // anything else means this isn't the block we expect
  }
  return fields;
}

/** Extract top-level field names declared directly on one `interface Name { ... }` block
 * (not fields inherited via `extends`) from the TS types file.
 */
function readInterfaceFieldNames(source: string, interfaceName: string): string[] {
  const headerRe = new RegExp(`export interface ${interfaceName}\\b[^{]*\\{`);
  const headerMatch = headerRe.exec(source);
  expect(headerMatch, `interface ${interfaceName} not found in media.ts`).not.toBeNull();

  const bodyStart = headerMatch!.index + headerMatch![0].length;
  const closeIndex = source.indexOf('\n}', bodyStart);
  expect(closeIndex, `closing brace for interface ${interfaceName} not found`).toBeGreaterThan(-1);

  const body = source.slice(bodyStart, closeIndex);
  return [...body.matchAll(/^\s*([a-zA-Z_][a-zA-Z0-9_]*)\??:/gm)].map((m) => m[1]);
}

function readTsErrorGroupState(): { present: Set<string>; strayErrorLike: string[] } {
  const source = readFileSync(TS_TYPES_PATH, 'utf-8');
  const allFields = [
    ...readInterfaceFieldNames(source, 'MediaFile'),
    ...readInterfaceFieldNames(source, 'MediaFileDetail'),
  ];

  const present = new Set(allFields);
  const strayErrorLike = allFields.filter(
    (name) => /error|message/i.test(name) && !EXPECTED_ERROR_GROUP_FIELDS.includes(name)
  );
  return { present, strayErrorLike };
}

describe('MediaFile error-reporting fields', () => {
  it('the backend error-handling block matches the pinned set (issue #841)', () => {
    expect(readBackendErrorGroupFields()).toEqual(EXPECTED_ERROR_GROUP_FIELDS);
  });

  it('the TS MediaFile/MediaFileDetail mirrors declare every backend error-group field', () => {
    const { present } = readTsErrorGroupState();
    for (const field of EXPECTED_ERROR_GROUP_FIELDS) {
      expect(present.has(field), `TS types are missing "${field}"`).toBe(true);
    }
  });

  it(
    'the TS mirrors carry no OTHER error/message-shaped field (no dead last_error_message ' +
      'or error_message left behind by a rename)',
    () => {
      const { strayErrorLike } = readTsErrorGroupState();
      expect(strayErrorLike).toEqual([]);
    }
  );
});
