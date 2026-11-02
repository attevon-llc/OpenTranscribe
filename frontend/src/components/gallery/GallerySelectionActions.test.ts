import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).englishLocale()
);
// The toolbar reads the selection from `$stores/gallery`; the double replaces a
// store vitest cannot load and lets each test set the selection itself.
vi.mock('$stores/gallery', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).galleryStoreMock()
);

import GallerySelectionActions from './GallerySelectionActions.svelte';

// The same per-file instance the gallery factory above returned.
const { galleryTriggers, resetGalleryDouble, selectFiles } = await import(
  '../../test-mocks/galleryToolbarMocks'
);

type ToolbarFiles = { uuid: string; status: string; can_delete?: boolean }[];

/** Renders the toolbar over `files` and opens its Organize menu. */
async function renderWithOrganizeMenu(files: ToolbarFiles = []) {
  render(GallerySelectionActions, { props: { files: files as never } });
  await fireEvent.click(
    screen.getByTitle('Add to collection or export transcripts for selected files')
  );
}

beforeEach(resetGalleryDouble);

describe('GallerySelectionActions — bulk tag entries', () => {
  it('offers one Tags entry in Organize, not separate add and remove', async () => {
    await renderWithOrganizeMenu();
    // The modal does both add and remove (and, for a single file, the full chip
    // editor), so three doors to one dialog were three things to explain.
    expect(screen.getByRole('button', { name: 'Add or Edit Tags' })).toBeInTheDocument();
    expect(screen.queryAllByRole('button', { name: /^(Add|Remove) Tag$/ })).toEqual([]);
  });

  it('names the tags entry for what it does to the selection', async () => {
    // A bare "Tags" beside "Add to Collection" did not say whether it added,
    // removed, or opened a manager. Both entries now describe the action they
    // perform on the selected files.
    await renderWithOrganizeMenu();
    expect(screen.getByRole('button', { name: 'Add or Edit Tags' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Tags' })).toBeNull();
  });

  it('applies tags to the selection and closes the menu', async () => {
    selectFiles(['a']);
    await renderWithOrganizeMenu([{ uuid: 'a', status: 'completed' }]);
    const tags = screen.getByRole('button', { name: 'Add or Edit Tags' });
    await fireEvent.click(tags);
    expect(galleryTriggers.triggerTags).toHaveBeenCalledTimes(1);
    // Choosing an entry closes the menu, like the neighbouring entries.
    expect(screen.queryByRole('button', { name: 'Add or Edit Tags' })).toBeNull();
  });

  it('disables both metadata entries when nothing is selected', async () => {
    // They act on the selection, so offering them with none invites the user to
    // "add to" or "edit" nothing. The toolbar's own Collections and Tags
    // buttons still open the managers, so nothing becomes unreachable.
    await renderWithOrganizeMenu();
    expect(screen.getByRole('button', { name: 'Add to Collection' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Add or Edit Tags' })).toBeDisabled();
  });

  it('enables them once a file is selected', async () => {
    selectFiles(['a']);
    await renderWithOrganizeMenu([{ uuid: 'a', status: 'completed' }]);
    expect(screen.getByRole('button', { name: 'Add to Collection' })).not.toBeDisabled();
    expect(screen.getByRole('button', { name: 'Add or Edit Tags' })).not.toBeDisabled();
  });
});

describe('GallerySelectionActions — actions that need a selection are disabled without one', () => {
  const completed: ToolbarFiles = [{ uuid: 'a', status: 'completed' }];

  it('disables every Organize export entry when nothing is selected', async () => {
    await renderWithOrganizeMenu();
    // Export writes a transcript for each selected file. With none selected it
    // produced an empty download and no error — the Process menu already gates
    // its entries on the selection, so this read as one toolbar with two rules.
    for (const name of ['Export SRT', 'Export WebVTT', 'Export Text']) {
      expect(screen.getByRole('button', { name })).toBeDisabled();
    }
  });

  it('enables the export entries once a completed file is selected', async () => {
    // The control: same markup, same menu, opposite outcome driven only by the
    // selection — so the assertion above cannot be passing on a permanently
    // disabled button.
    selectFiles(['a']);
    await renderWithOrganizeMenu(completed);
    for (const name of ['Export SRT', 'Export WebVTT', 'Export Text']) {
      expect(screen.getByRole('button', { name })).not.toBeDisabled();
    }
  });

  it('leaves export disabled when the selected file has no transcript yet', async () => {
    selectFiles(['b']);
    await renderWithOrganizeMenu([{ uuid: 'b', status: 'processing' }]);
    expect(screen.getByRole('button', { name: 'Export SRT' })).toBeDisabled();
  });

  it('disables Delete with no selection and enables it with one', async () => {
    const { unmount } = render(GallerySelectionActions, { props: { files: [] } });
    expect(screen.getByTitle('Permanently delete the selected files')).toBeDisabled();
    unmount();

    selectFiles(['a']);
    render(GallerySelectionActions, { props: { files: completed as never } });
    expect(screen.getByTitle('Permanently delete the selected files')).not.toBeDisabled();
  });
});

describe('GallerySelectionActions — Delete follows can_delete (issue #1103)', () => {
  const NOT_ALLOWED = "Only a file's owner or an organization admin can delete these files";

  it('disables Delete when no selected file is deletable by the caller', async () => {
    // An editor share no longer grants delete; the server refuses it, so the
    // toolbar must not offer it.
    selectFiles(['a']);
    render(GallerySelectionActions, {
      props: { files: [{ uuid: 'a', status: 'completed', can_delete: false }] as never },
    });
    expect(screen.getByTitle(NOT_ALLOWED)).toBeDisabled();
  });

  it('keeps Delete enabled when at least one selected file is deletable', async () => {
    selectFiles(['a', 'b']);
    render(GallerySelectionActions, {
      props: {
        files: [
          { uuid: 'a', status: 'completed', can_delete: false },
          { uuid: 'b', status: 'completed', can_delete: true },
        ] as never,
      },
    });
    expect(screen.getByTitle('Permanently delete the selected files')).not.toBeDisabled();
  });
});
