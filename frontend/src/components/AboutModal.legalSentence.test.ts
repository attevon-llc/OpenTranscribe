import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

/**
 * The licence and NOTICE sentences are ONE translatable string each, with the link placed where
 * the translator put the placeholder (issue #1222). They used to be `licensedUnder` + link +
 * `licensedUnderSuffix`, which fixed the link at the end of the sentence for every language.
 *
 * The fake `t` interpolates `{{name}}` from the options like i18next does, and serves a
 * sentence whose placeholder comes FIRST, which the old three-key layout could not render.
 */

const COPY = vi.hoisted(
  (): Record<string, string> => ({
    'about.legal.licensedUnder': '{{license}} governs this software.',
    'about.legal.licenseName': 'The Licence',
    'about.legal.thirdParty': 'See {{notice}} for third-party terms.',
    'about.legal.noticeName': 'NOTICE',
  })
);

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, string>) => string) => void) => {
      run((key: string, vars?: Record<string, string>) =>
        (COPY[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_m, name: string) => vars?.[name] ?? '')
      );
      return () => {};
    },
  },
}));
vi.mock('axios', () => ({
  default: { get: () => Promise.resolve({ data: { version: 'v0.5.0', git_sha: 'abc1234' } }) },
}));

import AboutModal from './AboutModal.svelte';

function legalLine(containing: string): HTMLElement {
  const lines = Array.from(document.querySelectorAll<HTMLElement>('.legal-section .legal-line'));
  const line = lines.find((el) => el.textContent?.includes(containing));
  if (!line) throw new Error(`no legal line containing ${containing}`);
  return line;
}

describe('AboutModal legal sentences (issue #1222)', () => {
  it('renders the licence link where the translation put the placeholder', () => {
    render(AboutModal, { props: { showModal: true } } as never);
    const line = legalLine('governs');
    const link = line.querySelector('a');
    expect(link?.textContent).toBe('The Licence');
    expect(link?.getAttribute('href')).toMatch(/\/blob\/master\/LICENSE$/);
    // The link comes first; nothing precedes it, the sentence continues after it.
    expect(line.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'The Licence governs this software.'
    );
    expect(line.firstElementChild).toBe(link);
  });

  it('renders the NOTICE link mid-sentence', () => {
    render(AboutModal, { props: { showModal: true } } as never);
    const line = legalLine('third-party');
    expect(line.querySelector('a')?.textContent).toBe('NOTICE');
    expect(line.textContent?.replace(/\s+/g, ' ').trim()).toBe('See NOTICE for third-party terms.');
  });
});
