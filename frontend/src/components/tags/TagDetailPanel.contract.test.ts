import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return {
    t: readable((key: string, opts?: Record<string, unknown>) =>
      opts ? `${key} ${JSON.stringify(opts)}` : key
    ),
  };
});

import TagDetailPanel from './TagDetailPanel.svelte';
import type { TagListEntry } from './TagList.svelte';
import type { TagImpact, TaggedFile } from '$lib/types/tag';

const UUID = 'aaaaaaaa-0000-0000-0000-000000000001';

function tagWith(extra: Partial<TagListEntry> = {}): TagListEntry {
  return {
    uuid: UUID,
    name: 'interviews',
    source: 'manual',
    usage_count: 12,
    can_write: true,
    ...extra,
  };
}

const impact: TagImpact = {
  tags: [],
  accessible_file_count: 3,
  total_file_count: 500,
};

const label = (key: string) => screen.queryByRole('button', { name: key });

describe('TagDetailPanel facts', () => {
  it('names the tag in a heading with labelled usage and origin facts', () => {
    render(TagDetailPanel, { props: { tag: tagWith({ source: 'auto_ai', usage_count: 1 }) } });
    expect(screen.getByRole('heading', { level: 2, name: 'interviews' })).toBeInTheDocument();
    expect(screen.getByText('tags.manager.detail.usageLabel').nextElementSibling).toHaveTextContent(
      'tags.manager.usage {"count":1}'
    );
    expect(
      screen.getByText('tags.manager.detail.originLabel').nextElementSibling
    ).toHaveTextContent('tags.manager.origin.auto');
  });

  it('explains a system tag with its badge and hint', () => {
    render(TagDetailPanel, { props: { tag: tagWith({ ownership: 'system' }) } });
    expect(screen.getByText('tags.manager.sharedBadge')).toBeInTheDocument();
    expect(screen.getByText('tags.manager.detail.sharedHint')).toBeInTheDocument();
    expect(screen.queryByText('tags.manager.detail.sharedWithMeHint')).toBeNull();
  });

  it('explains a shared_with_me tag with its badge and hint', () => {
    render(TagDetailPanel, { props: { tag: tagWith({ ownership: 'shared_with_me' }) } });
    expect(screen.getByText('tags.manager.sharedWithMeBadge')).toBeInTheDocument();
    expect(screen.getByText('tags.manager.detail.sharedWithMeHint')).toBeInTheDocument();
    expect(screen.queryByText('tags.manager.detail.sharedHint')).toBeNull();
  });

  it.each([['mine' as const], [undefined]])('shows no ownership note for %s', (ownership) => {
    render(TagDetailPanel, { props: { tag: tagWith({ ownership }) } });
    expect(screen.queryByText('tags.manager.sharedBadge')).toBeNull();
    expect(screen.queryByText('tags.manager.sharedWithMeBadge')).toBeNull();
  });
});

describe('TagDetailPanel write controls', () => {
  type Row = [
    string | undefined,
    boolean,
    boolean,
    { rename: boolean; share: boolean; promote: boolean; del: boolean },
  ];
  const matrix: Row[] = [
    // ownership, can_write, canPromote, expected controls
    ['mine', true, false, { rename: true, share: true, promote: false, del: true }],
    ['mine', true, true, { rename: true, share: true, promote: true, del: true }],
    ['system', false, false, { rename: false, share: false, promote: false, del: false }],
    ['system', true, true, { rename: true, share: false, promote: false, del: true }],
    ['shared_with_me', false, true, { rename: false, share: false, promote: false, del: false }],
    [undefined, true, true, { rename: true, share: true, promote: false, del: true }],
    // An org admin's colleague tag: not mine, yet the server accepts the write.
    ['shared_with_me', true, false, { rename: true, share: true, promote: false, del: true }],
    // A support grant: an owner-looking tag the server would refuse.
    ['mine', false, true, { rename: false, share: false, promote: true, del: false }],
  ];

  it.each(matrix)(
    'ownership %s, can_write %s, canPromote %s',
    (ownership, can_write, canPromote, want) => {
      render(TagDetailPanel, {
        props: {
          tag: tagWith({ ownership: ownership as TagListEntry['ownership'], can_write }),
          canPromote,
        },
      });
      expect(!!label('tags.manager.detail.rename')).toBe(want.rename);
      expect(!!label('tags.manager.share.button')).toBe(want.share);
      expect(!!label('tags.manager.action.promote')).toBe(want.promote);
      expect(!!label('tags.manager.action.delete')).toBe(want.del);
    }
  );

  it('emits share, promote and previewDelete from their buttons', async () => {
    const share = vi.fn();
    const promote = vi.fn();
    const previewDelete = vi.fn();
    render(TagDetailPanel, {
      props: { tag: tagWith({ ownership: 'mine' }), canPromote: true },
      events: { share, promote, previewDelete },
    });

    await fireEvent.click(label('tags.manager.share.button')!);
    await fireEvent.click(label('tags.manager.action.promote')!);
    await fireEvent.click(label('tags.manager.action.delete')!);

    expect(share).toHaveBeenCalledTimes(1);
    expect(promote).toHaveBeenCalledTimes(1);
    expect(previewDelete).toHaveBeenCalledTimes(1);
  });

  it('carries the tooltips of the write controls', () => {
    render(TagDetailPanel, { props: { tag: tagWith({ ownership: 'mine' }), canPromote: true } });
    expect(label('tags.manager.detail.rename')).toHaveAttribute(
      'title',
      'tags.manager.detail.renameTooltip'
    );
    expect(label('tags.manager.share.button')).toHaveAttribute(
      'title',
      'tags.manager.share.buttonTooltip'
    );
    expect(label('tags.manager.action.promote')).toHaveAttribute(
      'title',
      'tags.manager.promote.description'
    );
    expect(label('tags.manager.action.delete')).toHaveAttribute(
      'title',
      'tags.manager.detail.deleteTooltip'
    );
  });

  it.each(['rename', 'delete', 'promote'] as const)(
    'disables share, promote and delete while %s is in flight',
    (busy) => {
      render(TagDetailPanel, {
        props: { tag: tagWith({ ownership: 'mine' }), canPromote: true, busy },
      });
      expect(screen.getByRole('button', { name: 'tags.manager.share.button' })).toBeDisabled();
      expect(
        screen.getByRole('button', {
          name:
            busy === 'promote' ? 'tags.manager.action.promoting' : 'tags.manager.action.promote',
        })
      ).toBeDisabled();
      expect(screen.getByRole('button', { name: 'tags.manager.action.delete' })).toBeDisabled();
    }
  );

  it('leaves the controls enabled when idle', () => {
    render(TagDetailPanel, {
      props: { tag: tagWith({ ownership: 'mine' }), canPromote: true, busy: null },
    });
    for (const name of [
      'tags.manager.share.button',
      'tags.manager.action.promote',
      'tags.manager.action.delete',
    ]) {
      expect(screen.getByRole('button', { name })).toBeEnabled();
    }
  });
});

describe('TagDetailPanel inline rename', () => {
  async function startEditing(
    events: Record<string, (event: CustomEvent) => void> = {},
    props = {}
  ) {
    const view = render(TagDetailPanel, { props: { tag: tagWith(), ...props }, events });
    await fireEvent.click(label('tags.manager.detail.rename')!);
    return view;
  }

  it('swaps the heading for a labelled field prefilled with the name', async () => {
    await startEditing();
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull();
    expect(
      (screen.getByLabelText('tags.manager.detail.renameLabel') as HTMLInputElement).value
    ).toBe('interviews');
  });

  it('submits the trimmed value on Enter', async () => {
    const rename = vi.fn();
    await startEditing({ rename });
    const input = screen.getByLabelText('tags.manager.detail.renameLabel');
    await fireEvent.input(input, { target: { value: '  Interview  ' } });
    await fireEvent.keyDown(input, { key: 'Enter' });
    expect(rename).toHaveBeenCalledTimes(1);
    expect(rename.mock.calls[0][0].detail).toEqual({ name: 'Interview' });
  });

  it('cancels on Escape without emitting rename', async () => {
    const rename = vi.fn();
    await startEditing({ rename });
    const input = screen.getByLabelText('tags.manager.detail.renameLabel');
    await fireEvent.input(input, { target: { value: 'something else' } });
    await fireEvent.keyDown(input, { key: 'Escape' });
    expect(rename).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { level: 2, name: 'interviews' })).toBeInTheDocument();
    expect(screen.queryByLabelText('tags.manager.detail.renameLabel')).toBeNull();
  });

  it.each([
    ['the unchanged name', 'interviews'],
    ['only spaces', '   '],
    ['the name with padding', '  interviews '],
  ])('closes without an event for %s', async (_what, typed) => {
    const rename = vi.fn();
    const cancelRenameMerge = vi.fn();
    await startEditing({ rename, cancelRenameMerge });
    const input = screen.getByLabelText('tags.manager.detail.renameLabel');
    await fireEvent.input(input, { target: { value: typed } });
    await fireEvent.keyDown(input, { key: 'Enter' });
    expect(rename).not.toHaveBeenCalled();
    expect(cancelRenameMerge).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('tags.manager.detail.renameLabel')).toBeNull();
  });

  it('submits through the Rename button inside the editor, and cancel does not emit rename', async () => {
    const rename = vi.fn();
    await startEditing({ rename });
    await fireEvent.input(screen.getByLabelText('tags.manager.detail.renameLabel'), {
      target: { value: 'Calls' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.detail.rename' }));
    expect(rename.mock.calls.map((c) => c[0].detail)).toEqual([{ name: 'Calls' }]);

    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.action.cancel' }));
    expect(rename).toHaveBeenCalledTimes(1);
  });

  it('shows the busy label and locks the field while renaming', async () => {
    const view = await startEditing();
    await view.rerender({ tag: tagWith(), busy: 'rename' });
    expect(screen.getByRole('button', { name: 'tags.manager.detail.renaming' })).toBeDisabled();
    expect(screen.getByLabelText('tags.manager.detail.renameLabel')).toBeDisabled();
  });

  it('disables the editor submit while a merge warning is up', async () => {
    const view = await startEditing();
    await view.rerender({ tag: tagWith(), renameMergeImpact: impact });
    expect(screen.getByRole('button', { name: 'tags.manager.detail.rename' })).toBeDisabled();
  });

  it('cancelling the editor with a merge warning up also cancels the warning', async () => {
    const cancelRenameMerge = vi.fn();
    const view = await startEditing({ cancelRenameMerge });
    await view.rerender({ tag: tagWith(), renameMergeImpact: impact });
    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.action.cancel' }));
    expect(cancelRenameMerge).toHaveBeenCalledTimes(1);
  });

  it('cancelling the editor with no warning does not emit cancelRenameMerge', async () => {
    const cancelRenameMerge = vi.fn();
    await startEditing({ cancelRenameMerge });
    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.action.cancel' }));
    expect(cancelRenameMerge).not.toHaveBeenCalled();
  });
});

describe('TagDetailPanel merge-on-rename warning', () => {
  it('is absent without an impact and shows both counts and the note with one', async () => {
    const view = render(TagDetailPanel, { props: { tag: tagWith() } });
    expect(screen.queryByText('tags.manager.detail.mergeOnRenameTitle')).toBeNull();

    await view.rerender({ tag: tagWith(), renameMergeImpact: impact });
    expect(
      screen.getByRole('heading', { level: 3, name: 'tags.manager.detail.mergeOnRenameTitle' })
    ).toBeInTheDocument();
    expect(screen.getByText('tags.manager.detail.mergeOnRenameDescription')).toBeInTheDocument();
    expect(screen.getByText('tags.manager.impact.accessible {"count":3}')).toBeInTheDocument();
    expect(screen.getByText('tags.manager.impact.total {"count":500}')).toBeInTheDocument();
    expect(screen.getByText('tags.manager.impact.totalNote {"count":500}')).toBeInTheDocument();
  });

  it('disables confirm and swaps its label while the rename is running', () => {
    render(TagDetailPanel, {
      props: { tag: tagWith(), renameMergeImpact: impact, busy: 'rename' },
    });
    expect(screen.getByRole('button', { name: 'tags.manager.action.merging' })).toBeDisabled();
    expect(label('tags.manager.detail.confirmMergeRename')).toBeNull();
  });

  // D10 (8fb3cf289): the warning describes the name the last rename SENT, so confirm
  // dispatches that name, and dispatches nothing when no rename was submitted.
  it('keep editing emits cancelRenameMerge and confirm emits confirmRenameMerge for the submitted name', async () => {
    const rename = vi.fn();
    const cancelRenameMerge = vi.fn();
    const confirmRenameMerge = vi.fn();
    const view = render(TagDetailPanel, {
      props: { tag: tagWith() },
      events: { rename, cancelRenameMerge, confirmRenameMerge },
    });
    await fireEvent.click(label('tags.manager.detail.rename')!);
    await fireEvent.input(screen.getByLabelText('tags.manager.detail.renameLabel'), {
      target: { value: 'Calls' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.detail.rename' }));
    expect(rename.mock.calls.map((c) => c[0].detail)).toEqual([{ name: 'Calls' }]);
    await view.rerender({ tag: tagWith(), renameMergeImpact: impact });

    await fireEvent.click(label('tags.manager.detail.keepEditing')!);
    expect(cancelRenameMerge).toHaveBeenCalledTimes(1);
    expect(confirmRenameMerge).not.toHaveBeenCalled();

    await fireEvent.click(label('tags.manager.detail.confirmMergeRename')!);
    expect(confirmRenameMerge).toHaveBeenCalledTimes(1);
    expect(confirmRenameMerge.mock.calls[0][0].detail).toEqual({ name: 'Calls' });
  });

  it('confirms the previewed name, not text typed after the warning appeared', async () => {
    const cancelRenameMerge = vi.fn();
    const confirmRenameMerge = vi.fn();
    const view = render(TagDetailPanel, {
      props: { tag: tagWith() },
      events: { cancelRenameMerge, confirmRenameMerge },
    });
    await fireEvent.click(label('tags.manager.detail.rename')!);
    const field = screen.getByLabelText('tags.manager.detail.renameLabel');
    await fireEvent.input(field, { target: { value: 'Calls' } });
    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.detail.rename' }));
    await view.rerender({ tag: tagWith(), renameMergeImpact: impact });

    await fireEvent.input(field, { target: { value: 'Other' } });
    expect(cancelRenameMerge).toHaveBeenCalledTimes(1);

    await fireEvent.click(label('tags.manager.detail.confirmMergeRename')!);
    expect(confirmRenameMerge.mock.calls.map((c) => c[0].detail)).toEqual([{ name: 'Calls' }]);
  });

  it('confirm emits nothing when no rename was submitted', async () => {
    const confirmRenameMerge = vi.fn();
    render(TagDetailPanel, {
      props: { tag: tagWith(), renameMergeImpact: impact },
      events: { confirmRenameMerge },
    });
    await fireEvent.click(label('tags.manager.detail.confirmMergeRename')!);
    expect(confirmRenameMerge).not.toHaveBeenCalled();
  });
});

describe('TagDetailPanel file list', () => {
  const files: TaggedFile[] = [
    { uuid: 'f1', display_title: 'Standup notes', formatted_duration: '12:34' },
    { uuid: 'f2', display_title: 'Roadmap call', formatted_duration: null },
  ];

  it('shows a spinner and no list while loading', () => {
    render(TagDetailPanel, { props: { tag: tagWith(), files, fileTotal: 2, filesLoading: true } });
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByText('tags.manager.detail.noFiles')).toBeNull();
    expect(screen.getByText('tags.manager.detail.filesTitle')).toBeInTheDocument();
  });

  it('says there are no files for an empty list', () => {
    render(TagDetailPanel, { props: { tag: tagWith() } });
    expect(screen.getByText('tags.manager.detail.noFiles')).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('links each file to its page with the full title as tooltip and the duration when present', () => {
    render(TagDetailPanel, { props: { tag: tagWith(), files, fileTotal: 2 } });
    const links = screen.getAllByRole('link');
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/files/f1', '/files/f2']);
    expect(links.map((a) => a.getAttribute('title'))).toEqual(['Standup notes', 'Roadmap call']);
    expect(links[0]).toHaveTextContent('Standup notes');
    expect(links[0]).toHaveTextContent('12:34');
    expect(links[1].querySelector('.touches-meta')).toBeNull();
    expect(screen.getByRole('list').className).toContain('touches-list');
  });

  it('notes the files beyond the shown ones, and only when there are some', async () => {
    const view = render(TagDetailPanel, { props: { tag: tagWith(), files, fileTotal: 52 } });
    expect(screen.getByText('tags.manager.detail.moreFiles {"count":50}')).toBeInTheDocument();

    await view.rerender({ tag: tagWith(), files, fileTotal: 2 });
    expect(screen.queryByText(/tags\.manager\.detail\.moreFiles/)).toBeNull();

    await view.rerender({ tag: tagWith(), files, fileTotal: 0 });
    expect(screen.queryByText(/tags\.manager\.detail\.moreFiles/)).toBeNull();
  });
});

describe('TagDetailPanel previews', () => {
  it('shows the preview loading row only while previewLoading', async () => {
    const view = render(TagDetailPanel, { props: { tag: tagWith() } });
    expect(screen.queryByText('tags.manager.impact.loading')).toBeNull();
    await view.rerender({ tag: tagWith(), previewLoading: true });
    expect(screen.getByText('tags.manager.impact.loading')).toBeInTheDocument();
  });

  it('opens the confirmation dialog with the impact figures only once a delete preview exists', async () => {
    const view = render(TagDetailPanel, { props: { tag: tagWith() } });
    expect(screen.queryByRole('dialog')).toBeNull();

    await view.rerender({ tag: tagWith(), deletePreview: impact });
    expect(screen.getByText('tags.manager.impact.title')).toBeInTheDocument();
    expect(
      screen.getByText(
        'tags.manager.impact.deleteMessage {"name":"interviews","accessible":3,"total":500}'
      )
    ).toBeInTheDocument();
  });

  it('wires the confirmation buttons to confirmDelete and cancelDelete', async () => {
    const confirmDelete = vi.fn();
    const cancelDelete = vi.fn();
    render(TagDetailPanel, {
      props: { tag: tagWith(), deletePreview: impact },
      events: { confirmDelete, cancelDelete },
    });
    const confirm = document.querySelector('.modal-delete-button') as HTMLElement;
    const cancel = document.querySelector('.modal-cancel-button') as HTMLElement;
    expect(confirm).toHaveTextContent('tags.manager.action.delete');
    expect(cancel).toHaveTextContent('tags.manager.action.cancel');

    await fireEvent.click(cancel);
    expect(cancelDelete).toHaveBeenCalledTimes(1);
    expect(confirmDelete).not.toHaveBeenCalled();

    await fireEvent.click(confirm);
    expect(confirmDelete).toHaveBeenCalledTimes(1);
  });

  it('labels the confirm button while the delete runs', () => {
    render(TagDetailPanel, { props: { tag: tagWith(), deletePreview: impact, busy: 'delete' } });
    expect(document.querySelector('.modal-delete-button')).toHaveTextContent(
      'tags.manager.action.deleting'
    );
  });
});
