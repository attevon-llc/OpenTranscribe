import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

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
});
