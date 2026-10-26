import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import CollectionFormModal from './CollectionFormModal.svelte';

describe('CollectionFormModal', () => {
  it('goes straight from the title to the name field, with no tinted intro card', () => {
    render(CollectionFormModal, {
      idPrefix: 'collection',
      titleId: 'collection-title',
      title: 'Create collection',
      namePlaceholder: 'Name',
      submitLabel: 'Create',
      busyLabel: 'Creating',
      name: '',
      description: '',
      promptId: null,
    });

    expect(document.querySelector('.modal-intro')).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Create collection' })).toBeInTheDocument();
    expect(document.getElementById('collection-name')).not.toBeNull();
    // The three per-field hints stay.
    expect(screen.getByText('collectionsPanel.nameHint')).toBeInTheDocument();
    expect(screen.getByText('collectionsPanel.descriptionHint')).toBeInTheDocument();
  });

  const base = {
    idPrefix: 'collection',
    titleId: 'collection-title',
    title: 'Create collection',
    namePlaceholder: 'Name',
    submitLabel: 'Create',
    busyLabel: 'Creating',
    name: 'Board',
    description: '',
    promptId: null,
  };

  it('offers no visibility choice unless the caller may restrict an organization collection', () => {
    render(CollectionFormModal, base);
    expect(document.querySelector('input[type="radio"]')).toBeNull();
  });

  it('offers the two modes, defaulting to the whole organization, and follows the choice', async () => {
    render(CollectionFormModal, { ...base, showVisibility: true });

    const radios = Array.from(
      document.querySelectorAll<HTMLInputElement>(
        'input[type="radio"][name="collection-visibility"]'
      )
    );
    expect(radios.map((r) => r.value)).toEqual(['organization', 'restricted']);
    expect(radios.map((r) => r.checked)).toEqual([true, false]);
    expect(screen.getByText('collectionsPanel.visibility.restrictedHint')).toBeInTheDocument();

    await fireEvent.click(radios[1]);

    expect(radios.map((r) => r.checked)).toEqual([false, true]);
  });

  it("reflects the collection's current mode when editing", () => {
    render(CollectionFormModal, { ...base, showVisibility: true, visibility: 'restricted' });
    const checked = document.querySelector<HTMLInputElement>('input[type="radio"]:checked');
    expect(checked?.value).toBe('restricted');
  });
});
