/**
 * The list view's header cells reuse the body cells' column classes (`list-cell-speakers`,
 * `list-cell-duration`, ...). Those classes set their own `font-size` for the BODY text, and
 * because the header matched them too, the seven labels rendered at three different sizes
 * (12px, 14px and 11.2px). Svelte scopes component CSS, so jsdom cannot compute the cascade;
 * like `theme-parity.test.ts`, this reads the style source and pins the rule that resolves it.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';

const source = readFileSync(resolve(__dirname, 'VirtualList.svelte'), 'utf8');
const style = source.slice(source.indexOf('<style'));

function ruleBody(selector: string): string | null {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(style);
  return match ? match[1] : null;
}

describe('gallery list header typography', () => {
  it('forces every header cell to the header font size', () => {
    const body = ruleBody('.file-list-header .list-cell');
    expect(body).not.toBeNull();
    expect(body).toMatch(/font-size:\s*inherit/);
  });

  it('would be needed: body column rules do set their own font sizes', () => {
    // Control: if no body column set a size there would be nothing to override, and the
    // test above would be vacuous. These are the three that leaked into the header.
    for (const column of ['.list-cell-speakers', '.list-cell-status']) {
      expect(ruleBody(column)).toMatch(/font-size:/);
    }
    expect(style).toMatch(
      /\.list-cell-duration,\s*\n\s*\.list-cell-date,\s*\n\s*\.list-cell-size\s*\{[^}]*font-size:/
    );
  });
});
