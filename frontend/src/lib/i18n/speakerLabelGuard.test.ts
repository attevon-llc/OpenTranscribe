import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

// API speaker fields that can carry the diarizer placeholder (SPEAKER_NN). Rendering one
// of them in markup must go through translateSpeakerLabel (or a local helper wrapping it).
const RENDERED =
  /\{(?![:#/])[^}]*\b(primary_speakers|speaker_name|speaker_label|source\.speaker)\b[^}]*\}|title=\{[^}]*primary_speakers/;
// getSpeakerColor only keys a colour from the label; it renders no text.
const TRANSLATED = /translateSpeakerLabel|speakerNames\(|getSpeakerColor\(/;

function svelteFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return svelteFiles(p);
    return p.endsWith('.svelte') ? [p] : [];
  });
}

function markup(src: string): string[] {
  return src
    .replace(/<script[\s\S]*?<\/script>/g, (m) => m.replace(/[^\n]/g, ''))
    .replace(/<style[\s\S]*?<\/style>/g, (m) => m.replace(/[^\n]/g, ''))
    .split('\n');
}

describe('speaker placeholder labels are localised', () => {
  it('no component renders a raw API speaker label', () => {
    const root = join(__dirname, '..', '..');
    const offenders: string[] = [];
    for (const file of svelteFiles(join(root, 'components')).concat(
      svelteFiles(join(root, 'routes'))
    )) {
      markup(readFileSync(file, 'utf8')).forEach((line, i) => {
        if (RENDERED.test(line) && !TRANSLATED.test(line)) {
          offenders.push(`${file.slice(root.length + 1)}:${i + 1}: ${line.trim()}`);
        }
      });
    }
    expect(offenders).toEqual([]);
  });
});
