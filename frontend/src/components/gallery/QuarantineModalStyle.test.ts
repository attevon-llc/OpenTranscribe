/**
 * The Quarantine dialog's legal-hold checkbox rendered its text as "a weird narrow column"
 * (issue #1243, item 15): the global `input { width: 100%; padding: 8px 12px }` in
 * styles/form-elements.css also matches a checkbox, so inside the flex row the box claimed
 * 105-168px and squeezed the label text. jsdom cannot cascade Svelte's scoped CSS, so this
 * reads the style source (as VirtualListHeaderStyle.test.ts does) and pins the rules that
 * override it. The measured proof, in a real browser, was the checkbox at 16px and the text
 * filling the rest of the row at 420 and 1440 wide, light/dark, en/ar.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';

const source = readFileSync(resolve(__dirname, 'QuarantineModal.svelte'), 'utf8');
const style = source.slice(source.indexOf('<style'));
const globalForm = readFileSync(resolve(__dirname, '../../styles/form-elements.css'), 'utf8');

function ruleBody(selector: string): string | null {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(style);
  return match ? match[1] : null;
}

describe('quarantine dialog legal-hold checkbox row', () => {
  it('control: the global input rule really does stretch every input to full width', () => {
    // If this stops being true the overrides below are dead weight and should go.
    expect(globalForm).toMatch(/\ninput,\s*\ntextarea\s*\{[^}]*width:\s*100%/);
  });

  it('pins the checkbox to its own size so it cannot claim the row', () => {
    const body = ruleBody('.legal-hold-field input');
    expect(body).not.toBeNull();
    expect(body).toMatch(/width:\s*1rem/);
    expect(body).toMatch(/flex:\s*0\s+0\s+auto/);
    expect(body).toMatch(/padding:\s*0\b/);
  });

  it('lets the label text take the remaining width and wrap', () => {
    const body = ruleBody('.legal-hold-field span');
    expect(body).not.toBeNull();
    expect(body).toMatch(/flex:\s*1\s+1\s+auto/);
    expect(body).toMatch(/min-width:\s*0/);
  });
});
