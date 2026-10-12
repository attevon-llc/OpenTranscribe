/**
 * Every tab strip inside a settings section is the shared sticky `ui/Tabs`: one mechanism, so a
 * tall panel never scrolls the strip away and no section can drift back to a hand-rolled,
 * non-pinned copy (Authentication and Chat once did).
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const settingsDir = resolve(process.cwd(), 'src/components/settings');

function svelteFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return svelteFiles(path);
    return entry.name.endsWith('.svelte') ? [path] : [];
  });
}

const files = svelteFiles(settingsDir).map((path) => ({
  name: path.slice(settingsDir.length + 1),
  source: readFileSync(path, 'utf8'),
}));

describe('settings tab strips', () => {
  it('finds the settings panels it is meant to guard', () => {
    expect(files.length).toBeGreaterThan(30);
    expect(files.some((f) => /<Tabs\b/.test(f.source))).toBe(true);
  });

  it('never hand-rolls a tablist or a .tabs row', () => {
    const offenders = files
      .filter((f) => /role="tablist"|class="tabs[\s"]/.test(f.source))
      .map((f) => f.name);
    expect(offenders).toEqual([]);
  });

  it('opts every <Tabs> into the sticky strip', () => {
    const offenders: string[] = [];
    for (const f of files) {
      for (const match of f.source.matchAll(/<Tabs\b[\s\S]*?\/>/g)) {
        if (!/\ssticky(\s|=|\/|>)/.test(match[0])) offenders.push(f.name);
      }
    }
    expect(offenders).toEqual([]);
  });
});
