/**
 * Every animation in the notification panel stops under `prefers-reduced-motion`. jsdom
 * cannot evaluate media queries, so this reads the component sources: an `animation:` or
 * `transition:` declaration with no reduced-motion block that resets it is the defect (the
 * progress shimmer ran unconditionally before the panel was split).
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join, basename } from 'node:path';

const dir = __dirname;
const files = [
  ...readdirSync(dir)
    .filter((f) => f.endsWith('.svelte'))
    .map((f) => join(dir, f)),
  join(dir, '..', 'NotificationsPanel.svelte'),
];

function problems(source: string): string[] {
  const style = source.slice(Math.max(0, source.indexOf('<style')));
  const reducedAt = style.indexOf('prefers-reduced-motion');
  const reduced = reducedAt === -1 ? '' : style.slice(reducedAt);
  const out: string[] = [];
  if (/\banimation:\s*(?!none)/.test(style) && !/animation:\s*none/.test(reduced))
    out.push('animation');
  if (/\btransition:\s*(?!none)/.test(style) && !/transition:\s*none/.test(reduced))
    out.push('transition');
  return out;
}

describe('reduced motion', () => {
  it('the detector flags an unguarded animation and accepts a guarded one', () => {
    expect(problems('<style>.a { animation: spin 1s; }</style>')).toEqual(['animation']);
    expect(
      problems(
        '<style>.a { animation: spin 1s; transition: width 1s; } @media (prefers-reduced-motion: reduce) { .a { animation: none; transition: none; } }</style>'
      )
    ).toEqual([]);
  });

  it('every panel component resets its animations and transitions', () => {
    expect(files.length).toBeGreaterThanOrEqual(8);
    const offenders = files.flatMap((file) =>
      problems(readFileSync(file, 'utf8')).map((kind) => `${basename(file)}: ${kind}`)
    );
    expect(offenders).toEqual([]);
  });
});
