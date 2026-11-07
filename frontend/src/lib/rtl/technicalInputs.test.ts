import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { globSync } from 'node:fs';
import { technicalInputsMissingLtr } from './technicalInputs';

describe('technicalInputsMissingLtr (detector)', () => {
  it('fires on a technical input without dir="ltr"', () => {
    const src = `<input id="ldap_ca_cert_file" type="text" bind:value={x} />
<input id="u" type="url" bind:value={y}
  class="a" />`;
    expect(technicalInputsMissingLtr(src)).toHaveLength(2);
  });

  it('stays clean for ltr technical inputs, prose inputs and non-text inputs', () => {
    const src = `<input id="ldap_ca_cert_file" type="text" dir="ltr" />
<input id="config-name" type="text" />
<input id="scim-token-name" type="text" />
<input id="x-label" type="text" />
<input id="oidc_enabled" type="checkbox" />`;
    expect(technicalInputsMissingLtr(src)).toEqual([]);
  });
});

// Settings/admin panels plus the media-URL form: every input holding a URL, host, path, DN,
// filter, e-mail, credential or regex must keep its value left-to-right under `ar`.
const FILES = [
  ...globSync('src/components/settings/**/*.svelte'),
  'src/components/upload/MediaUrlPanel.svelte',
];

describe('technical inputs keep dir="ltr" in RTL locales', () => {
  it('scans a non-trivial set of panels', () => {
    expect(FILES.length).toBeGreaterThan(30);
  });

  it.each(FILES)('%s', (file) => {
    expect(technicalInputsMissingLtr(readFileSync(file, 'utf8'))).toEqual([]);
  });
});
