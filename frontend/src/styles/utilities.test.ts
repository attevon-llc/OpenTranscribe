import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { SRC, walkSource } from './sourceTree';

describe('.sr-only utility', () => {
  it('is defined once, in styles/utilities.css, and nowhere else', () => {
    const definers = walkSource(SRC, /\.(svelte|css)$/).filter((f) =>
      /^\s*\.sr-only\s*\{/m.test(fs.readFileSync(f, 'utf8'))
    );
    expect(definers.map((f) => path.relative(SRC, f))).toEqual(['styles/utilities.css']);
  });

  it('uses the clip pattern that keeps the text readable by assistive tech', () => {
    const css = fs.readFileSync(path.join(SRC, 'styles/utilities.css'), 'utf8');
    for (const decl of ['position: absolute', 'width: 1px', 'height: 1px', 'overflow: hidden']) {
      expect(css).toContain(decl);
    }
    expect(css).not.toMatch(/display:\s*none|visibility:\s*hidden/);
  });
});
