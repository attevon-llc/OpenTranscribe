import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect } from 'vitest';

const ar: Record<string, string> = JSON.parse(
  readFileSync(join(__dirname, 'locales', 'ar.json'), 'utf8')
);

const LRI = '⁦';
const PDI = '⁩';

// A figure with its percent sign: in a right-to-left run the sign can detach and read "%99".
const PERCENT = /(\{\{\w+\}\}|\d+(?:\.\d+)?)\s?[%٪]/g;

function unisolated(value: string, pattern: RegExp): string[] {
  const bad: string[] = [];
  for (const m of value.matchAll(pattern)) {
    const i = m.index ?? 0;
    const before = value[i - 1];
    const after = value[i + m[0].length];
    if (before !== LRI || after !== PDI) bad.push(m[0]);
  }
  return bad;
}

describe('Arabic copy isolates left-to-right tokens', () => {
  it('detects an unisolated token and accepts an isolated one', () => {
    expect(unisolated('بنسبة {{percent}}%', PERCENT)).toEqual(['{{percent}}%']);
    expect(unisolated(`بنسبة ${LRI}{{percent}}%${PDI}`, PERCENT)).toEqual([]);
  });

  it('wraps every percentage so it cannot render as "%99"', () => {
    const offenders = Object.entries(ar).filter(([, v]) => unisolated(v, PERCENT).length);
    expect(offenders.map(([k]) => k)).toEqual([]);
  });
});
