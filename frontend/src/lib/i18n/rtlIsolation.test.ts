import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect } from 'vitest';

const ar: Record<string, string> = JSON.parse(
  readFileSync(join(__dirname, 'locales', 'ar.json'), 'utf8')
);

const LRI = '⁦';
const PDI = '⁩';

// A figure with its percent sign, and technical tokens (file names, environment variables)
// that must read left-to-right inside Arabic copy: ".env" otherwise renders as "env.".
const PERCENT = /(\{\{\w+\}\}|\d+(?:\.\d+)?)\s?[%٪]/g;
const TECHNICAL =
  /(?<![\w.])(\.env|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?:=[\w.-]+)?|docker-compose\.[\w.]+\.yml)(?![\w])/g;

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
    expect(unisolated('في ملف .env ثم', TECHNICAL)).toEqual(['.env']);
    expect(unisolated(`في ملف ${LRI}.env${PDI} ثم`, TECHNICAL)).toEqual([]);
    expect(unisolated('بنسبة {{percent}}%', PERCENT)).toEqual(['{{percent}}%']);
    expect(unisolated(`بنسبة ${LRI}{{percent}}%${PDI}`, PERCENT)).toEqual([]);
  });

  it('wraps every percentage so it cannot render as "%99"', () => {
    const offenders = Object.entries(ar).filter(([, v]) => unisolated(v, PERCENT).length);
    expect(offenders.map(([k]) => k)).toEqual([]);
  });

  it('wraps ".env", environment variables and compose file names so ".env" cannot render as "env."', () => {
    const offenders = Object.entries(ar).filter(([, v]) => unisolated(v, TECHNICAL).length);
    expect(offenders.map(([k]) => k)).toEqual([]);
  });
});
