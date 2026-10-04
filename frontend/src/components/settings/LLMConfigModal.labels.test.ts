/**
 * Issue #971: the model-name <label> wrapped the "discover models" buttons, so
 * one label named two controls (input + button) and the input's accessible name
 * absorbed the button text. Buttons must live beside the label, not inside it.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import type { ProviderDefaults } from '../../lib/api/llmSettings';

vi.mock('../../stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import LLMConfigModal from './LLMConfigModal.svelte';

const providers: ProviderDefaults[] = (['ollama', 'openai', 'anthropic'] as const).map(
  (provider) => ({
    provider,
    default_model: 'm',
    default_base_url: 'http://x/v1',
    requires_api_key: provider !== 'ollama',
    supports_custom_url: true,
    max_context_length: 8192,
    description: provider,
  })
);

describe('LLMConfigModal labels (issue #971)', () => {
  for (const provider of ['ollama', 'openai', 'anthropic']) {
    it(`model-name label has exactly the input and no nested button (${provider})`, async () => {
      render(LLMConfigModal, {
        props: { show: true, editingConfig: null, supportedProviders: providers },
      } as never);
      await fireEvent.change(screen.getByLabelText('llm.provider'), {
        target: { value: provider },
      });

      const input = screen.getByLabelText('llm.modelName') as HTMLInputElement;
      expect(input.id).toBe('model-name');
      expect(document.querySelector('.discover-models-btn')).not.toBeNull();
      expect(document.querySelectorAll('label button')).toHaveLength(0);
    });
  }
});
