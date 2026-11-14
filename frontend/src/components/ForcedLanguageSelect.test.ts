/**
 * The forced-language picker names languages in the UI language. The backend sends
 * `code -> English name`; showing those names verbatim left every locale reading English.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'de' })
);

import ForcedLanguageSelect from './ForcedLanguageSelect.svelte';

describe('ForcedLanguageSelect', () => {
  it('lists languages by their name in the UI language', () => {
    render(ForcedLanguageSelect, {
      props: { languages: { auto: 'Auto-detect', es: 'Spanish', fr: 'French' } },
    });

    expect(screen.getByRole('option', { name: 'Spanisch' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Französisch' })).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: 'settings.transcription.defaultSourceAuto' })
    ).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Spanish' })).toBeNull();
  });
});
