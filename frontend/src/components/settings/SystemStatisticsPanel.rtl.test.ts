/**
 * In Arabic the statistics printed "GB 12.00" and "%0": sizes, units and the GPU name are
 * technical values that must read left to right inside right-to-left labels. jsdom does no
 * bidi layout, so the rule is read from the markup: each value after a label sits in a
 * `<bdi dir="ltr">`.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';

const markup = readFileSync(resolve(__dirname, 'SystemStatisticsPanel.svelte'), 'utf8').split(
  '<style>'
)[0];

describe('SystemStatisticsPanel values in right-to-left languages', () => {
  it('isolates every labelled size, GPU and utilisation value left-to-right', () => {
    const labelled = markup
      .split('\n')
      .filter((line) =>
        /settings\.statistics\.(total|used|free|gpu|gpuUtilization|gpuTemperature)'\)\}: /.test(
          line
        )
      );
    expect(labelled.length).toBeGreaterThanOrEqual(7);
    for (const line of labelled) expect(line, line).toContain('<bdi dir="ltr">');
  });

  it('isolates the memory "used / total" pair and the success rate', () => {
    expect(markup).toMatch(/<bdi dir="ltr">\{stats\.system\?\.memory\?\.used/);
    expect(markup).toMatch(/<bdi dir="ltr">\{stats\.tasks\?\.success_rate \|\| 0\}%<\/bdi>/);
  });
});
