import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      let out = en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

import TagDetailPanel from './TagDetailPanel.svelte';
import type { TagImpact, TagReviewResult } from '$lib/types/tag';

const UUID = 'aaaaaaaa-0000-0000-0000-000000000001';

const tag = {
  uuid: UUID,
  name: 'interviews',
  source: 'manual',
  usage_count: 12,
  awaiting_review: false,
};

const impact: TagImpact = {
  tags: [{ uuid: UUID, name: 'interviews', accessible_file_count: 3, total_file_count: 500 }],
  accessible_file_count: 3,
  total_file_count: 500,
};

const rejectResult: TagReviewResult = {
  tags: [
    {
      uuid: UUID,
      name: 'interviews',
      outcome: 'rejected',
      removed_association_count: 7,
      retained_association_count: 2,
      tag_removed: false,
    },
  ],
  removed_association_count: 7,
  retained_association_count: 2,
  deleted_uuids: [],
  applied: false,
  impact,
};

describe('TagDetailPanel', () => {
  it('shows the tag with its usage and origin', () => {
    render(TagDetailPanel, { props: { tag } });
    expect(screen.getByRole('heading', { name: 'interviews' })).toBeInTheDocument();
    expect(screen.getByText('Used on 12 files')).toBeInTheDocument();
    expect(screen.getByText('Added by hand')).toBeInTheDocument();
  });

  it('offers accept and reject only for tags awaiting review', async () => {
    const { rerender } = render(TagDetailPanel, { props: { tag } });
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();

    await rerender({ tag: { ...tag, awaiting_review: true } });
    expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument();
  });

  describe('inline rename', () => {
    it('submits the typed name and restores the prior value on cancel', async () => {
      const rename = vi.fn();
      render(TagDetailPanel, { props: { tag }, events: { rename } });

      await fireEvent.click(screen.getByRole('button', { name: 'Rename' }));
      const input = screen.getByLabelText('Tag name') as HTMLInputElement;
      expect(input.value).toBe('interviews');

      await fireEvent.input(input, { target: { value: 'Interviews ' } });
      await fireEvent.click(screen.getByRole('button', { name: 'Rename' }));
      expect(rename.mock.calls[0][0].detail).toEqual({ name: 'Interviews' });

      // Cancelling the edit itself discards the typing (GroupDetailPanel's rule).
      await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      await fireEvent.click(screen.getByRole('button', { name: 'Rename' }));
      expect((screen.getByLabelText('Tag name') as HTMLInputElement).value).toBe('interviews');
    });

    it('shows the merge impact under a still-populated field and keeps the typing on cancel', async () => {
      const cancelRenameMerge = vi.fn();
      const confirmRenameMerge = vi.fn();
      const { rerender } = render(TagDetailPanel, {
        props: { tag },
        events: { cancelRenameMerge, confirmRenameMerge },
      });

      await fireEvent.click(screen.getByRole('button', { name: 'Rename' }));
      const input = screen.getByLabelText('Tag name') as HTMLInputElement;
      await fireEvent.input(input, { target: { value: 'Interview' } });

      await rerender({ tag, renameMergeImpact: impact });
      expect(
        screen.getByRole('heading', { name: 'That name already belongs to another tag' })
      ).toBeInTheDocument();
      expect(screen.getByText('3 files you can see')).toBeInTheDocument();
      expect(screen.getByText('500 files in total')).toBeInTheDocument();
      expect((screen.getByLabelText('Tag name') as HTMLInputElement).value).toBe('Interview');

      await fireEvent.click(screen.getByRole('button', { name: 'Merge into that tag' }));
      expect(confirmRenameMerge.mock.calls[0][0].detail).toEqual({ name: 'Interview' });

      await fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }));
      expect(cancelRenameMerge).toHaveBeenCalled();
      // The typed name survives the near miss — the user does not retype it.
      expect((screen.getByLabelText('Tag name') as HTMLInputElement).value).toBe('Interview');
    });
  });

  describe('destructive actions', () => {
    it('asks for the impact before deleting, then applies it', async () => {
      const previewDelete = vi.fn();
      const confirmDelete = vi.fn();
      const { rerender } = render(TagDetailPanel, {
        props: { tag },
        events: { previewDelete, confirmDelete },
      });

      await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
      expect(previewDelete).toHaveBeenCalledTimes(1);
      expect(confirmDelete).not.toHaveBeenCalled();
      expect(screen.queryByText('500 files in total')).toBeNull();

      await rerender({ tag, deletePreview: impact });
      expect(screen.getByRole('heading', { name: 'Before this is applied' })).toBeInTheDocument();
      // Both counts, with the global one called out as what actually changes.
      expect(screen.getByText('3 files you can see')).toBeInTheDocument();
      expect(screen.getByText('500 files in total')).toBeInTheDocument();
      expect(
        screen.getByText(
          'This operation affects all 500 files, including files you cannot see.'
        )
      ).toBeInTheDocument();

      const deleteButtons = screen.getAllByRole('button', { name: 'Delete' });
      const confirm = deleteButtons[deleteButtons.length - 1];
      await fireEvent.click(confirm);
      expect(confirmDelete).toHaveBeenCalledTimes(1);
    });

    it('reports removed and retained associations before rejecting', async () => {
      const previewReject = vi.fn();
      const { rerender } = render(TagDetailPanel, {
        props: { tag: { ...tag, awaiting_review: true } },
        events: { previewReject },
      });

      await fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
      expect(previewReject).toHaveBeenCalledTimes(1);

      await rerender({ tag: { ...tag, awaiting_review: true }, rejectPreview: rejectResult });
      expect(screen.getByText('7 auto-applied links removed')).toBeInTheDocument();
      expect(screen.getByText('2 hand-applied links kept')).toBeInTheDocument();
      expect(
        screen.getByText(
          'Hand-applied tags are kept, so a tag that has any survives the reject.'
        )
      ).toBeInTheDocument();
    });

    it('shows a pending label and disables the confirming control while a delete is in flight', async () => {
      render(TagDetailPanel, { props: { tag, deletePreview: impact, busy: 'delete' } });
      const pending = screen.getByRole('button', { name: 'Deleting…' });
      expect(pending).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    });
  });
});
