/**
 * The composer is ONE rounded box. `styles/form-elements.css` paints every <textarea>
 * (radius, and on :focus a `--field-focus-ring` box-shadow), at zero specificity, so a
 * component that embeds a bare textarea inside its own bordered shell must neutralise every
 * property that sheet paints. The composer reset `border` and `background` but not
 * `box-shadow`/`border-radius`, so focusing the input drew a second, square-ish ring inside
 * the rounded shell.
 *
 * The property list is derived from form-elements.css itself, so a paint property added
 * there later is caught here too. Source-level, like `styles/formFields.test.ts`: jsdom
 * does not cascade the global sheet against Svelte's scoped rules.
 */
import { describe, expect, it } from 'vitest';
import fs from 'fs';
import path from 'path';

const read = (rel: string) => fs.readFileSync(path.resolve(__dirname, rel), 'utf8');
const strip = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '');

/** Paint properties the global sheet sets on a text field, in any state. */
function globalFieldPaint(): Set<string> {
  const css = strip(read('../../styles/form-elements.css'));
  const props = new Set<string>();
  for (const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!/\btextarea\b/.test(rule[1]) || rule[1].includes('::')) continue;
    for (const decl of rule[2].matchAll(/^\s*([a-z-]+)\s*:/gm)) {
      if (/^(border(-color|-radius)?|box-shadow|background(-color)?|outline)$/.test(decl[1])) {
        props.add(decl[1].replace(/-color$/, ''));
      }
    }
  }
  return props;
}

describe('ChatComposer draws a single outline', () => {
  const style = strip(read('ChatComposer.svelte').split('<style')[1] ?? '');
  const textareaRule = /(?:^|\n)\s*textarea\s*\{([^}]*)\}/.exec(style)?.[1] ?? '';

  it('finds the global paint and the composer rule (guards against matching nothing)', () => {
    expect(globalFieldPaint()).toEqual(
      new Set(['border', 'border-radius', 'background', 'box-shadow', 'outline'])
    );
    expect(textareaRule).toContain('flex');
  });

  it('neutralises every property the global field sheet paints on the inner textarea', () => {
    const missing = [...globalFieldPaint()].filter(
      (prop) =>
        !new RegExp(`(^|;|\\n)\\s*${prop}\\s*:\\s*(none|0|transparent)\\b`).test(textareaRule)
    );
    expect(missing).toEqual([]);
  });

  it('keeps a visible focus state on the shell instead', () => {
    expect(style).toMatch(
      /\.input-shell:focus-within\s*\{[^}]*box-shadow:\s*var\(--field-focus-ring\)/
    );
  });
});
