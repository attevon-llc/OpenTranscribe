/**
 * Every export and copy failure on the file page must tell the user (issue #1278, #564's T5).
 *
 * These catch blocks used to end in `console.error` alone, so a 409 (redaction scan unfinished)
 * or a 503 (policy unavailable) left a button that silently did nothing. The route's children
 * are stubbed in `page.test.ts`, which cannot reach a menu click, so this pins the wiring
 * structurally instead: each failure path logs AND raises a toast whose key comes from
 * `exportErrorKey`, which `exportErrors.test.ts` proves maps each status and reason.
 */
import { describe, it, expect } from 'vitest';
import page from './+page.svelte?raw';

const FAILURE_LOGS = [
  "console.error('Error exporting transcript:', error);",
  "console.error('Error copying transcript:', error);",
  "console.error('Error exporting document:', error);",
];

describe('file page export failures', () => {
  it.each(FAILURE_LOGS)('%s is followed by an error toast', (log) => {
    const at = page.indexOf(log);
    expect(at, `${log} is no longer in the page`).toBeGreaterThan(-1);
    const after = page.slice(at, at + 400);
    expect(after).toContain('toastStore.error($t(exportErrorKey(error)));');
  });

  it('has no other export catch that only logs', () => {
    const logs = [...page.matchAll(/console\.error\('Error (?:exporting|copying) [a-z]+:'/g)];
    expect(logs.length).toBe(FAILURE_LOGS.length);
  });

  it('never builds a document in the browser when the job fails', () => {
    const start = page.indexOf('async function handleDocumentExportConfirm');
    const end = page.indexOf('async function handleSaveSpeakerNames');
    const body = page.slice(start, end);
    expect(body).toContain('startDocumentExport');
    expect(body).not.toContain('new Blob');
    expect(body).not.toContain('createObjectURL');
  });
});
