/**
 * A focus indicator must use `--focus-ring-color`, never `--primary-color`.
 *
 * `--primary-color` is #2563eb in both themes' fill role; on the dark card it is 2.83:1, below the
 * 3:1 that WCAG 2.2 SC 1.4.11 (https://www.w3.org/TR/WCAG22/#non-text-contrast) asks of a UI
 * component's visual information. `--focus-ring-color` is the boundary role: #2563eb in light,
 * #60a5fa (5.75:1) in dark. Six field-focus rules kept the old token after the global sweep
 * because nothing scanned for them; this does.
 *
 * Fails when a rule whose selector has `:focus`, `:focus-visible` or `:focus-within` sets
 * `outline`, `outline-color`, `border-color` or a `border*` shorthand with `--primary-color`, or
 * when a `--*focus*` custom property is set from `--primary-color` (svelte-multiselect's
 * `--sms-focus-border` is consumed as a focus border).
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

const SRC = path.resolve(__dirname, '..');

/** `<file>::<selector>` -> written reason. Deliberately empty: there is no legitimate exception. */
const ALLOWLIST: Record<string, string> = {};

function walk(dir: string, out: string[] = []): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (/\.(svelte|css)$/.test(e.name)) out.push(p);
  }
  return out;
}

function styleText(file: string): string {
  let text = fs.readFileSync(file, 'utf8');
  if (file.endsWith('.svelte')) {
    text = [...text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
  }
  return text.replace(/\/\*[\s\S]*?\*\//g, '');
}

const FOCUS_SELECTOR = /:focus(?:-visible|-within)?(?![\w-])/;
const BORDER_OR_OUTLINE =
  /(?<![-\w])(?:outline(?:-color)?|border(?:-(?:top|right|bottom|left|inline|block)(?:-\w+)?)?(?:-color)?)\s*:[^;]*var\(--primary-color\)/;
const FOCUS_VAR = /(?<![-\w])--[\w-]*focus[\w-]*\s*:[^;]*var\(--primary-color\)/;

export function findOffenders(files: string[]): string[] {
  const bad: string[] = [];
  for (const file of files) {
    const rel = path.relative(SRC, file).split(path.sep).join('/');
    for (const m of styleText(file).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const selector = m[1].trim().replace(/\s+/g, ' ');
      const body = m[2];
      const hit =
        (FOCUS_SELECTOR.test(selector) && BORDER_OR_OUTLINE.test(body)) || FOCUS_VAR.test(body);
      const key = `${rel}::${selector}`;
      if (hit && !(key in ALLOWLIST)) bad.push(key);
    }
  }
  return bad;
}

describe('focus indicators use --focus-ring-color', () => {
  const files = walk(SRC);

  it('scans a real tree', () => {
    expect(files.length).toBeGreaterThan(100);
  });

  it('no focus rule draws its indicator with --primary-color', () => {
    expect(findOffenders(files)).toEqual([]);
  });

  it('detector fires on a focus border and stays quiet on the token', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'focus-guard-'));
    const f = path.join(tmp, 'x.css');
    fs.writeFileSync(
      f,
      '.a:focus-within { border-color: var(--primary-color); }\n' +
        '.b:focus-visible { outline: 2px solid var(--primary-color); }\n' +
        '.c { --sms-focus-border: 1px solid var(--primary-color); }\n' +
        '.d:focus { border-color: var(--focus-ring-color); }\n' +
        '.e:hover { border-color: var(--primary-color); }\n' +
        '.f:focus { background: var(--primary-color); }\n'
    );
    const found = findOffenders([f]).map((k) => k.split('::')[1]);
    fs.rmSync(tmp, { recursive: true });
    expect(found).toEqual(['.a:focus-within', '.b:focus-visible', '.c']);
  });
});
