import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import AISuggestionsDropdown from './AISuggestionsDropdown.svelte';

describe('AISuggestionsDropdown with a repeated suggestion', () => {
  it('renders both chips when the LLM suggests the same tag twice', async () => {
    const { container } = render(AISuggestionsDropdown, {
      props: {
        type: 'tag',
        suggestions: [
          { name: 'budget', confidence: 0.9 },
          { name: 'budget', confidence: 0.6 },
        ],
      },
    });
    await fireEvent.click(container.querySelector('button') as HTMLElement);
    expect(container.querySelectorAll('.suggestion-chip')).toHaveLength(2);
  });
});
