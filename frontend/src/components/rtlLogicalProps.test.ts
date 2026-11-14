import { describe, it, expect } from 'vitest';
import search from './search/SearchAutocomplete.svelte?raw';
import clusters from './speakers/ClustersTab.svelte?raw';
import backup from './settings/BackupSettings.svelte?raw';
import dirsync from './settings/DirectorySyncSettings.svelte?raw';
import page from '/src/routes/search/+page.svelte?raw';
import media from './upload/MediaUrlPanel.svelte?raw';

// UI-pass R2/R6/R7: physical padding/margin does not mirror in Arabic (MDN: CSS logical properties).
const css = (s: string) => s.slice(s.indexOf('<style'));

describe('RTL-safe spacing (UI-pass R2/R6/R7)', () => {
  it('search + cluster inputs and their clear buttons use logical edges', () => {
    expect(css(search)).not.toMatch(/padding-right|right: 0.5rem/);
    expect(css(clusters)).toContain('padding-inline: var(--space-3) var(--space-8)');
  });
  it('search filter drawer and operator hint use logical edges (UFR2)', () => {
    expect(css(page)).not.toMatch(/(^|\s)left:|transition: left|border-right/m);
    expect(css(page)).toContain('inset-inline-start: -100%');
    expect(css(search)).not.toMatch(/(^|\s)(left|right): 0;/m);
    expect(page).not.toContain('align="right"');
  });
  it('settings sub-option indents use margin-inline-start', () => {
    expect(css(backup)).not.toMatch(/margin:[^;]*var\(--space-12\)/);
    expect(css(dirsync)).not.toMatch(/margin:[^;]*var\(--space-12\)/);
  });
  it('media URL wrapper is dir=ltr so input padding and paste button agree', () => {
    expect(media).toMatch(/class="url-input-wrapper" dir="ltr"/);
  });
});
