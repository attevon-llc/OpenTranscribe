import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

/**
 * UI pass N7: a native <option> cannot wrap, so a select narrower than its longest label cuts it
 * (pt "... · tradução automática", ar Whisper model label). The selects take the full width of
 * their container (MDN, <select> styling: size with inline-size / max-inline-size). jsdom cannot
 * lay out, so this reads the style source.
 */
const FILES: [string, string][] = [
  ['LanguageSettings.svelte', '.form-select'],
  ['../PerFileModelSelect.svelte', '.model-select'],
];

describe('long-label native selects use the full container width', () => {
  it.each(FILES)('%s %s', (file, sel) => {
    const src = fs.readFileSync(path.resolve(__dirname, file), 'utf8');
    const style = src.slice(src.indexOf('<style'));
    const esc = sel.replace('.', '\\.');
    const body = style.match(new RegExp(`${esc}\\s*\\{([^}]*)\\}`))?.[1] ?? '';
    expect(body).toMatch(/inline-size:\s*100%/);
    expect(body).toMatch(/max-inline-size:\s*100%/);
    expect(body).not.toMatch(/max-width:\s*\d+px/);
  });
});
