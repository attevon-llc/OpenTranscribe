/**
 * DEFECT THIS CATCHES (G4): `TruncatedText` used `$: showMoreText = showMoreText
 * || $t('common.seeMore')` — a reactive statement that assigns the translated
 * string BACK INTO the prop. Once `showMoreText` becomes truthy (the first
 * render, since the prop starts as `""`), the `||` short-circuits on every
 * later re-run, so a locale change never overwrites it again: the label
 * latches to whatever language was active at first render. The fix derives a
 * separate `resolvedShowMoreText`/`resolvedShowLessText` instead of writing
 * over the prop, so a locale switch is picked up.
 */

import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Writable } from 'svelte/store';

vi.mock('$stores/locale', async () => {
  const { writable } = await import('svelte/store');
  return { t: writable((key: string) => key) };
});

const { t: mockT } = (await import('$stores/locale')) as unknown as {
  t: Writable<(key: string) => string>;
};
import TruncatedText from './TruncatedText.svelte';

const translations: Record<string, Record<string, string>> = {
  en: { 'common.seeMore': 'See more', 'common.seeLess': 'See less' },
  fr: { 'common.seeMore': 'Voir plus', 'common.seeLess': 'Voir moins' },
};

function translatorFor(locale: 'en' | 'fr') {
  return (key: string) => translations[locale][key] ?? key;
}

describe('TruncatedText locale-derived labels', () => {
  it('updates the "see more" label when the active locale changes after render', async () => {
    mockT.set(translatorFor('en'));
    const longText = 'x'.repeat(200);

    const { getByRole } = render(TruncatedText, { props: { text: longText, maxLength: 150 } });

    expect(getByRole('button').textContent).toBe('See more');

    // Simulate a locale switch — the store's translator function changes.
    mockT.set(translatorFor('fr'));
    await tick();

    expect(getByRole('button').textContent).toBe('Voir plus');
  });

  it('control: an explicitly-provided showMoreText prop is used regardless of locale', async () => {
    mockT.set(translatorFor('en'));
    const longText = 'x'.repeat(200);

    const { getByRole } = render(TruncatedText, {
      props: { text: longText, maxLength: 150, showMoreText: 'Custom label' },
    });

    expect(getByRole('button').textContent).toBe('Custom label');

    mockT.set(translatorFor('fr'));
    await tick();

    expect(getByRole('button').textContent).toBe('Custom label');
  });
});
