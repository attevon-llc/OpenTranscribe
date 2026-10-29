/**
 * One confirm-dialog state machine.
 *
 * Five screens each hand-rolled a title/message/callback trio plus a `handleConfirmModalConfirm`
 * and `handleConfirmModalCancel`. They drifted (callback-then-close vs close-then-callback, one
 * clearing the cancel callback and one not). `$lib/confirmDialog.ts` owns that now.
 *
 * This fails when any non-test source file other than the helper mentions
 * `handleConfirmModalConfirm`.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative, resolve } from 'path';

const SRC = resolve(__dirname, '..');
const OWNER = 'lib/confirmDialog.ts';
const HAND_ROLLED = /\bhandleConfirmModalConfirm\b/;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(svelte|ts|js)$/.test(full) && !/\.test\.(ts|js)$/.test(full)) out.push(full);
  }
  return out;
}

describe('HAND_ROLLED (the detector itself)', () => {
  it('fires on a hand-rolled confirm handler and on its use as an event handler', () => {
    expect(HAND_ROLLED.test('function handleConfirmModalConfirm() { cb(); }')).toBe(true);
    expect(HAND_ROLLED.test('<ConfirmationModal on:confirm={handleConfirmModalConfirm} />')).toBe(
      true
    );
  });

  it('stays quiet for the helper API and for an unrelated handler', () => {
    expect(HAND_ROLLED.test('on:confirm={confirmDialog.confirm}')).toBe(false);
    expect(HAND_ROLLED.test('function handleConfirmModalOpen() {}')).toBe(false);
  });
});

describe('confirm dialog convention', () => {
  const files = walk(SRC).map((f) => ({ rel: relative(SRC, f).replace(/\\/g, '/'), full: f }));

  it('finds the source tree it is meant to scan', () => {
    expect(files.length).toBeGreaterThan(100);
    expect(files.some((f) => f.rel === OWNER)).toBe(true);
  });

  it('no file hand-rolls handleConfirmModalConfirm', () => {
    const bad = files
      .filter((f) => f.rel !== OWNER && HAND_ROLLED.test(readFileSync(f.full, 'utf8')))
      .map((f) => f.rel);
    expect(bad, 'Use createConfirmDialog() from $lib/confirmDialog').toEqual([]);
  });
});
