import { describe, it, expect, vi } from 'vitest';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { SUPPORTED_LANGUAGES } = await import('$lib/i18n/languages');
  return {
    SUPPORTED_LANGUAGES,
    locale: (await import('svelte/store')).writable('en'),
    t: {
      subscribe: (run: (value: (key: string) => string) => void) => {
        run((key: string) => (key === 'settings.language.machineTranslated' ? 'MT-LABEL' : key));
        return () => {};
      },
    },
  };
});
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

import LanguageSettings from './LanguageSettings.svelte';

describe('LanguageSettings machine-translation notice', () => {
  it('keeps every option label free of the marker', () => {
    const { container } = render(LanguageSettings);
    const options = Array.from(container.querySelectorAll('select#ui-language option'));
    expect(options).toHaveLength(12);
    for (const opt of options) expect(opt.textContent).not.toContain('MT-LABEL');
  });

  it('shows the note for the selected machine-translated locale only', async () => {
    const { locale } = await import('$stores/locale');
    locale.set('en');
    const { container } = render(LanguageSettings);
    const select = container.querySelector('select#ui-language') as HTMLSelectElement;
    expect(container.querySelector('[data-testid="language-mt-note"]')).toBeNull();
    expect(select.getAttribute('aria-describedby')).not.toContain('ui-language-mt');
    locale.set('pt');
    await tick();
    const note = container.querySelector('[data-testid="language-mt-note"]') as HTMLElement;
    expect(note.textContent).toContain('MT-LABEL');
    expect(select.getAttribute('aria-describedby')).toContain(note.id);
    expect(get(locale)).toBe('pt');
  });
});
