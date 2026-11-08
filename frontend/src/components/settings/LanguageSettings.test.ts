import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { SUPPORTED_LANGUAGES } = await import('$lib/i18n/languages');
  return {
    SUPPORTED_LANGUAGES,
    locale: { subscribe: (run: (v: string) => void) => (run('en'), () => {}), set: vi.fn() },
    t: {
      subscribe: (run: (value: (key: string) => string) => void) => {
        run((key: string) => (key === 'settings.language.machineTranslated' ? 'MT-LABEL' : key));
        return () => {};
      },
    },
  };
});
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn() } }));

import LanguageSettings from './LanguageSettings.svelte';

describe('LanguageSettings machine-translation notice', () => {
  it('marks every non-English locale, and only those, as machine-translated', () => {
    const { container } = render(LanguageSettings);
    const options = Array.from(container.querySelectorAll('select#ui-language option'));
    expect(options).toHaveLength(12);
    for (const opt of options) {
      const marked = (opt.textContent ?? '').includes('MT-LABEL');
      expect(marked, (opt as HTMLOptionElement).value).toBe(
        (opt as HTMLOptionElement).value !== 'en'
      );
    }
  });
});
