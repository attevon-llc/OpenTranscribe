/**
 * On a held file every edit control the freeze refuses stays VISIBLE, disabled, with the
 * reason printed beside it (not only in a tooltip a disabled button never shows), and the
 * things a hold allows (a new comment) stay enabled (legal-hold plan, Lane D). Each case has
 * an unfrozen control so "disabled" cannot pass on a control that is always disabled.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import type { MediaFileDetail } from '$lib/types/media';

vi.mock('$stores/locale', async () => {
  const mocks = await import('$testMocks/galleryToolbarMocks');
  const { readable } = await import('svelte/store');
  return { ...mocks.englishLocale(), locale: readable('en') };
});

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn().mockResolvedValue({ data: { prompts: [] } }) },
}));

import FrozenContextTestHost from './FrozenContextTestHost.svelte';
import FileActionButtons from '$components/fileDetail/FileActionButtons.svelte';
import RedactionControls from '$components/fileDetail/RedactionControls.svelte';
import CommentItem from '$components/comments/CommentItem.svelte';
import SummaryActions from '$components/SummaryActions.svelte';
import TagsSection from '$components/TagsSection.svelte';
import CollectionsSection from '$components/CollectionsSection.svelte';

const REASON = 'Locked by a legal hold';

const completed = {
  uuid: 'f1',
  filename: 'a.mp4',
  status: 'completed',
  transcript_segments: [{ uuid: 's1' }],
  has_summary: false,
  tags: [{ uuid: 't1', name: 'board' }],
} as unknown as MediaFileDetail;

function host(component: unknown, props: Record<string, unknown>, frozen: boolean) {
  return render(FrozenContextTestHost, { props: { component, props, frozen } } as never);
}

/** The disabled control names the visible reason as its description. */
function expectExplained(button: HTMLElement) {
  expect(button).toBeDisabled();
  const id = button.getAttribute('aria-describedby');
  expect(id).toBeTruthy();
  expect(document.getElementById(id as string)).toHaveTextContent(REASON);
}

describe('frozen controls on a held file', () => {
  it('summary generate and reprocess: disabled with the reason beside them', () => {
    const props = { file: completed, canEdit: true, llmAvailable: true };
    host(FileActionButtons, props, true);
    expectExplained(screen.getByRole('button', { name: /Generate/ }));
    expectExplained(screen.getByRole('button', { name: /Reprocess/ }));
    expect(screen.getByTestId('frozen-note')).toBeVisible();
  });

  it('summary generate and reprocess: enabled when the file is not held', () => {
    host(FileActionButtons, { file: completed, canEdit: true, llmAvailable: true }, false);
    expect(screen.getByRole('button', { name: /Generate/ })).toBeEnabled();
    expect(screen.getByRole('button', { name: /Reprocess/ })).toBeEnabled();
    expect(screen.queryByTestId('frozen-note')).toBeNull();
  });

  it('redaction rescan: disabled with the reason; viewing the original still works', () => {
    const props = {
      showRedactionToggle: true,
      canViewOriginal: true,
      redactionEnabled: true,
    };
    host(RedactionControls, props, true);
    expectExplained(screen.getByRole('button', { name: /Re-?scan|Rescan/i }));
    expect(screen.getByRole('button', { name: /original/i })).toBeEnabled();
  });

  it('an existing comment: edit and delete disabled with the reason', () => {
    const comment = { uuid: 'c1', text: 'note', timestamp: null, user: { uuid: 'u1' } };
    render(CommentItem, {
      props: { comment, isAuthor: true, frozen: true, save: async () => true },
    } as never);
    expectExplained(screen.getByRole('button', { name: 'Edit' }));
    expectExplained(screen.getByRole('button', { name: 'Delete' }));
  });

  it('an existing comment: edit and delete enabled when not held', () => {
    const comment = { uuid: 'c1', text: 'note', timestamp: null, user: { uuid: 'u1' } };
    render(CommentItem, {
      props: { comment, isAuthor: true, frozen: false, save: async () => true },
    } as never);
    expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
  });

  it('summary actions: generate disabled with the reason', () => {
    const props = { summary: null, llmAvailable: true, summaryStatus: 'pending' };
    host(SummaryActions, props, true);
    expectExplained(screen.getByRole('button', { name: /Generate/ }));
  });

  it('tags: read-only chips and the reason instead of the editor', () => {
    host(TagsSection, { file: completed, canEdit: true, isTagsExpanded: true }, true);
    expect(screen.getAllByText('board').length).toBeGreaterThan(0);
    expect(screen.getByTestId('frozen-note')).toHaveTextContent(REASON);
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('collections: read-only chips and the reason instead of the editor', () => {
    const props = {
      fileId: 'f1',
      collections: [{ uuid: 'c1', name: 'Matter 7' }],
      isExpanded: true,
    };
    host(CollectionsSection, props, true);
    expect(screen.getAllByText('Matter 7').length).toBeGreaterThan(0);
    expect(screen.getByTestId('frozen-note')).toHaveTextContent(REASON);
    expect(screen.queryByRole('textbox')).toBeNull();
  });
});
