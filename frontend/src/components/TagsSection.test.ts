/**
 * Regression guard for #335.
 *
 * `TagsSection` used to carry a handler commented "Re-emit the event to parent
 * component" that never called `dispatch` — it mutated the `file` prop and
 * stopped. The file-detail page listens for `tagsUpdated` to assign `file.tags`,
 * which is what re-renders the section, so tag edits never propagated. These
 * tests pin the forwarding: the editor's `tagsUpdated` must reach the page
 * unchanged, and the section must not mutate the page-owned `file` object.
 *
 * The tag payload is `Tag[]` on the wire now (#326) — `MediaFileDetail.tags`
 * carries objects, not bare names — so the fixture uses tag objects.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';
import type { MediaFileDetail } from '$lib/types/media';
import TagsSection from './TagsSection.svelte';

const axiosMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('$lib/axios', () => ({ default: axiosMock }));

function fileWithTags(): MediaFileDetail {
  return {
    uuid: 'file-uuid-1',
    filename: 'alpha.mp4',
    status: 'completed',
    upload_time: '2026-01-01T00:00:00Z',
    tags: [{ uuid: 'tag-uuid-1', name: 'alpha', can_write: true }],
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
      props: { file, isTagsExpanded: true, canEdit: true },
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
      props: { file, isTagsExpanded: true, canEdit: true },
      events: { tagsUpdated: vi.fn() },
    });

    await fireEvent.click(container.querySelector('.tag-remove') as HTMLButtonElement);

    await waitFor(() => expect(axiosMock.delete).toHaveBeenCalledTimes(1));
    expect(file.tags).toEqual([{ uuid: 'tag-uuid-1', name: 'alpha', can_write: true }]);
  });
});

describe('TagsSection editing gate', () => {
  beforeEach(() => {
    axiosMock.get.mockReset().mockResolvedValue({ data: [] });
    axiosMock.post.mockReset().mockResolvedValue({ data: {} });
    axiosMock.delete.mockReset().mockResolvedValue({ data: {} });
  });

  it('shows a non-editor the tags read-only, with no attach or detach controls', () => {
    const { container, getByText } = render(TagsSection, {
      props: { file: fileWithTags(), isTagsExpanded: true, canEdit: false },
    });

    expect(container.querySelector('.tags-readonly')).toBeTruthy();
    expect(getByText('alpha', { selector: '.tags-readonly .preview-chip' })).toBeTruthy();
    expect(container.querySelector('.tag-remove')).toBeNull();
    expect(container.querySelector('.tag-input')).toBeNull();
    expect(container.querySelector('.tag-add-button')).toBeNull();
  });

  it('defaults to read-only when the page passes no permission', () => {
    const { container } = render(TagsSection, {
      props: { file: fileWithTags(), isTagsExpanded: true },
    });

    expect(container.querySelector('.tag-remove')).toBeNull();
    expect(container.querySelector('.tag-input')).toBeNull();
  });

  it('gives an editor the attach and detach controls', () => {
    const { container } = render(TagsSection, {
      props: { file: fileWithTags(), isTagsExpanded: true, canEdit: true },
    });

    expect(container.querySelector('.tags-readonly')).toBeNull();
    expect(container.querySelector('.tag-remove')).toBeTruthy();
    expect(container.querySelector('.tag-input')).toBeTruthy();
  });
});
