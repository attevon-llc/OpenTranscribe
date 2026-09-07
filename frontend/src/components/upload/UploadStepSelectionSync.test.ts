/**
 * The Tags and Collections steps show the same selection twice: as removable
 * chips, and as checkboxes in the list below. Those two views must never
 * disagree.
 *
 * They did. `checked={isSelected(x)}` names a FUNCTION, not the selection array,
 * so Svelte never registered `selectedTags` / `selectedCollections` as a
 * dependency of that binding and never re-ran it. Removing a chip filtered the
 * array — the chip row re-rendered because it iterates the array directly — but
 * the checkbox kept its stale `checked` state, leaving an item that looked
 * selected in one place and deselected in the other.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => ({ default: { post: vi.fn(), get: vi.fn() } }));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.mock('$lib/utils/apiError', () => ({ getErrorStatus: () => 500 }));
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

const boxes = (c: HTMLElement) =>
  Array.from(c.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[];

describe('upload step selection stays in sync between chips and checkboxes', () => {
  beforeEach(() => vi.clearAllMocks());

  it('collections: removing a chip also unchecks its row', async () => {
    const selected = [{ uuid: 'u1', name: 'Interviews' }];
    const { container } = render(UploadStepCollections, {
      props: {
        selectedCollections: selected,
        availableCollections: [
          { uuid: 'u1', name: 'Interviews' },
          { uuid: 'u2', name: 'Standups' },
        ],
      },
    });

    await waitFor(() => expect(boxes(container).length).toBe(2));
    expect(boxes(container)[0].checked).toBe(true);

    const remove = container.querySelector('.chip-remove') as HTMLElement;
    expect(remove).toBeTruthy();
    await fireEvent.click(remove);

    await waitFor(() => expect(boxes(container)[0].checked).toBe(false));
  });

  it('tags: removing a chip also unchecks its row', async () => {
    const { container } = render(UploadStepTags, {
      props: {
        selectedTags: ['meeting'],
        availableTags: [
          { uuid: 't1', name: 'meeting', usage_count: 3 },
          { uuid: 't2', name: 'interview', usage_count: 1 },
        ],
      },
    });

    await waitFor(() => expect(boxes(container).length).toBeGreaterThan(0));
    expect(boxes(container)[0].checked).toBe(true);

    const remove = container.querySelector('.chip-remove') as HTMLElement;
    expect(remove).toBeTruthy();
    await fireEvent.click(remove);

    await waitFor(() => expect(boxes(container)[0].checked).toBe(false));
  });
});
