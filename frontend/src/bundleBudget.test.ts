/**
 * Issue #1131 — the root layout chunk is paid by every page load, so its size is gated.
 * `scripts/check-bundle-budget.mjs` runs on the built output (`postbuild`); this pins the
 * budget helpers and the layout's lazy-loading contract at source level.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  ROOT_LAYOUT_GZIP_BUDGET,
  gzipSize,
  staticImports,
  withinBudget,
} from '../scripts/check-bundle-budget.mjs';

describe('bundle budget helpers', () => {
  it('accepts a size at the budget and rejects one byte over', () => {
    expect(withinBudget(ROOT_LAYOUT_GZIP_BUDGET)).toBe(true);
    expect(withinBudget(ROOT_LAYOUT_GZIP_BUDGET + 1)).toBe(false);
  });

  it('measures gzip, not raw, size', () => {
    const repetitive = Buffer.from('a'.repeat(10_000));
    expect(gzipSize(repetitive)).toBeLessThan(repetitive.length / 10);
  });
});

describe('root layout static imports', () => {
  const layout = readFileSync(resolve(process.cwd(), 'src/routes/+layout.svelte'), 'utf8');
  const staticImports = layout.match(/^\s*import\s+[^;]*?from\s+['"][^'"]+['"]/gm) ?? [];

  it.each([
    'SettingsModal',
    'FirstRunWizard',
    'QuotaExceededModal',
    'LegalGateModal',
    'IdleTimeoutDialog',
  ])('does not statically import %s', (name) => {
    expect(staticImports.filter((line) => line.includes(name))).toEqual([]);
  });
});

describe('staticImports', () => {
  it('follows static imports but not dynamic ones', () => {
    const src =
      'import{a}from"../chunks/A.js";import"./B.js";const m=()=>import("../chunks/C.js");export{a};';
    expect(staticImports(src)).toEqual(['../chunks/A.js', './B.js']);
  });
});
