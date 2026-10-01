/**
 * The model picker renders read-only when the deployment owns the model choice
 * (`transcription.model_choice` off; the server ignores a requested model then).
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import UploadStepModel from './UploadStepModel.svelte';

describe('UploadStepModel', () => {
  it('offers the model select by default', () => {
    const { container } = render(UploadStepModel);
    expect(container.querySelector('#whisper-model-select')).not.toBeNull();
    expect(container.querySelector('[data-testid="model-managed"]')).toBeNull();
  });

  it('renders a read-only managed note when model choice is locked', () => {
    const { container } = render(UploadStepModel, { props: { modelChoiceEnabled: false } });
    expect(container.querySelector('#whisper-model-select')).toBeNull();
    expect(container.querySelector('[data-testid="model-managed"]')?.textContent).toBe(
      'uploader.modelManaged'
    );
  });
});
