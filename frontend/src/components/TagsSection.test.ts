/**
 * Regression guard for #335.
 *
 * `TagsSection` used to carry a handler commented "Re-emit the event to parent
 * component" that never called `dispatch` — it mutated the `file` prop and
 * stopped. The file-detail page listens for `tagsUpdated` to update its own
 * state and `reactiveFile`, so tag edits never propagated. These tests pin the
 * forwarding: the editor's `tagsUpdated` must reach the page unchanged, and the
 * section must not mutate the page-owned `file` object.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';
import TagsSection from './TagsSection.svelte';

const axiosMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('$lib/axios', () => ({ default: axiosMock }));

function fileWithTags() {
  return {
    uuid: 'file-uuid-1',
    tags: [{ uuid: 'tag-uuid-1', name: 'alpha' }],
  };
}

describe('TagsSection (#335 tag update forwarding)', () => {
  beforeEach(() => {
    axiosMock.get.mockReset().mockResolvedValue({ data: [] });
    axiosMock.post.mockReset().mockResolvedValue({ data: {} });
    axiosMock.delete.mockReset().mockResolvedValue({ data: {} });
  });

  it('re-emits `tagsUpdated` from the editor to its parent', async () => {
    const onTagsUpdated = vi.fn();
    const file = fileWithTags();

    const { container } = render(TagsSection, {
      props: { file, isTagsExpanded: true },
      events: { tagsUpdated: onTagsUpdated },
    });

    const removeButton = container.querySelector('.tag-remove') as HTMLButtonElement;
    expect(removeButton).toBeTruthy();
    await fireEvent.click(removeButton);

    await waitFor(() => expect(onTagsUpdated).toHaveBeenCalledTimes(1));
    expect((onTagsUpdated.mock.calls[0][0] as CustomEvent).detail).toEqual({ tags: [] });
  });

  it('leaves the page-owned `file.tags` untouched — the page applies the update', async () => {
    const file = fileWithTags();

    const { container } = render(TagsSection, {
      props: { file, isTagsExpanded: true },
      events: { tagsUpdated: vi.fn() },
    });

    await fireEvent.click(container.querySelector('.tag-remove') as HTMLButtonElement);

    await waitFor(() => expect(axiosMock.delete).toHaveBeenCalledTimes(1));
    expect(file.tags).toEqual([{ uuid: 'tag-uuid-1', name: 'alpha' }]);
  });
});
