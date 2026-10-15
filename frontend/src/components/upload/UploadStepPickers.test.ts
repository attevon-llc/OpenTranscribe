/**
 * The upload wizard's Tags and Collections steps are pickers, not managers, so
 * they use the same `SearchableMultiSelect` the filter sidebar uses (UX review
 * item 4), and "Create collection" opens the gallery's own
 * `CollectionFormModal` rather than a private inline input (item 6).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.mock('$lib/utils/apiError', () => ({ getErrorStatus: () => 409, getErrorMessage: () => 'x' }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import UploadStepCollections from './UploadStepCollections.svelte';
import UploadStepTags from './UploadStepTags.svelte';

const collections = [
  { uuid: 'u1', name: 'Interviews' },
  { uuid: 'u2', name: 'Standups' },
];
const tags = [
  { uuid: 't1', name: 'meeting', usage_count: 3 },
  { uuid: 't2', name: 'interview', usage_count: 1 },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockResolvedValue({ data: { prompts: [] } });
  mockAxios.post.mockResolvedValue({ data: { uuid: 'u3', name: 'Fresh' } });
});

describe('pickers use the sidebar multi-select', () => {
  it('tags step renders a SearchableMultiSelect', () => {
    const { container } = render(UploadStepTags, { props: { availableTags: tags } });
    expect(container.querySelector('.multiselect-container')).not.toBeNull();
    expect(container.querySelector('.item-filter')).toBeNull();
  });

  it('collections step renders a SearchableMultiSelect', () => {
    const { container } = render(UploadStepCollections, {
      props: { availableCollections: collections },
    });
    expect(container.querySelector('.multiselect-container')).not.toBeNull();
    expect(container.querySelector('.item-filter')).toBeNull();
  });

  it('picking a collection in the dropdown selects it', async () => {
    const { container } = render(UploadStepCollections, {
      props: { availableCollections: collections },
    });

    await fireEvent.click(container.querySelector('.multiselect-toggle') as HTMLElement);
    // Portaled to <body> by anchoredPanel.
    const box = document.querySelector('.options-list input[type="checkbox"]') as HTMLElement;
    await fireEvent.click(box);

    await waitFor(() => expect(container.querySelectorAll('.selected-chips .chip').length).toBe(1));
    expect(container.querySelector('.selected-chips .chip')?.textContent).toContain('Interviews');
  });
});

describe('Create collection reuses the gallery modal', () => {
  it('opens CollectionFormModal instead of an inline input', async () => {
    const { container, getByRole } = render(UploadStepCollections, {
      props: { availableCollections: collections },
    });
    expect(container.querySelector('.create-input')).toBeNull();

    await fireEvent.click(container.querySelector('.create-btn') as HTMLElement);

    const dialog = getByRole('dialog');
    expect(dialog.getAttribute('aria-labelledby')).toBe('create-collection-title');
  });

  it('submitting the modal creates the collection and selects it', async () => {
    const { container, getByRole } = render(UploadStepCollections, {
      props: { availableCollections: collections },
    });

    await fireEvent.click(container.querySelector('.create-btn') as HTMLElement);
    const dialog = getByRole('dialog');
    const nameInput = dialog.querySelector('#collection-name') as HTMLInputElement;
    await fireEvent.input(nameInput, { target: { value: 'Fresh' } });
    await fireEvent.submit(dialog.querySelector('form') as HTMLElement);

    await waitFor(() => expect(mockAxios.post).toHaveBeenCalled());
    expect(mockAxios.post.mock.calls[0][0]).toBe('/collections');
    expect(mockAxios.post.mock.calls[0][1].name).toBe('Fresh');
    await waitFor(() =>
      expect(container.querySelector('.selected-chips .chip')?.textContent).toContain('Fresh')
    );
  });
});
