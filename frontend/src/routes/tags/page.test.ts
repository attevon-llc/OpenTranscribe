import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/svelte';

/**
 * Route-level tests: the page is the coordinator, so the states that only exist
 * because of a fetch (skeleton / error+retry / four distinct empty states) and
 * the detail-vs-bulk swap can only be exercised here.
 */
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

const api = vi.hoisted(() => ({
  listTags: vi.fn(),
  listTagCollisions: vi.fn(),
  getTagImpact: vi.fn(),
  getTagReviewImpact: vi.fn(),
  acceptTags: vi.fn(),
  rejectTags: vi.fn(),
  deleteTags: vi.fn(),
  mergeTags: vi.fn(),
  renameTag: vi.fn(),
  promoteTags: vi.fn(),
}));

vi.mock('$lib/api/tags', () => api);

// `canPromote` reads the session role. The admin-only controls are cosmetic —
// `POST /tags/promote` re-checks server-side — but the page must not offer them
// to a plain user, so the role is driven explicitly per test.
const auth = vi.hoisted(() => ({ role: 'admin' as string }));

vi.mock('$stores/auth', async () => {
  const { readable } = await import('svelte/store');
  return { user: readable({ uuid: 'u1', email: 'a@example.com', get role() { return auth.role; } }) };
});

import TagsPage from './+page.svelte';

const UUID_A = 'aaaaaaaa-0000-0000-0000-000000000001';
const UUID_B = 'bbbbbbbb-0000-0000-0000-000000000002';

const allTags = [
  {
    uuid: UUID_A,
    name: 'interviews',
    source: 'manual',
    usage_count: 12,
    awaiting_review: false,
    is_shared: false,
  },
  {
    uuid: UUID_B,
    name: 'roadmap',
    source: 'auto_ai',
    usage_count: 4,
    awaiting_review: true,
    is_shared: true,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  auth.role = 'admin';
  api.listTags.mockResolvedValue(allTags);
  api.listTagCollisions.mockResolvedValue([]);
  api.getTagImpact.mockResolvedValue({
    tags: [],
    accessible_file_count: 3,
    total_file_count: 500,
  });
  api.getTagReviewImpact.mockResolvedValue({
    tags: [],
    removed_association_count: 7,
    retained_association_count: 2,
    deleted_uuids: [],
    applied: false,
    impact: { tags: [], accessible_file_count: 3, total_file_count: 500 },
  });
});

/**
 * The tag rows are a `role=listbox` of `role=option`s — but so is every native
 * `<select>` on the page, including the ownership-scope picker. Query inside the
 * listbox so a new control never silently changes what these counts mean.
 */
function tagRows(): HTMLElement[] {
  return within(screen.getByRole('listbox')).getAllByRole('option');
}

describe('tag manager route', () => {
  it('lists the tags the backend returned', async () => {
    render(TagsPage);
    await waitFor(() => expect(tagRows()).toHaveLength(2));
    expect(screen.getByText('interviews')).toBeInTheDocument();
    expect(screen.getByText('Used on 12 files')).toBeInTheDocument();
  });

  it('renders an error with retry — never the empty state — when the fetch fails', async () => {
    api.listTags.mockRejectedValueOnce({ response: { data: { detail: 'tag query exploded' } } });
    render(TagsPage);

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByText('tag query exploded')).toBeInTheDocument();
    // The empty state would tell the user their library is empty. It must not appear.
    expect(screen.queryByText('No tags yet')).toBeNull();

    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(tagRows()).toHaveLength(2));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  describe('selection', () => {
    it('shows the detail pane for one tag and the bulk summary for several', async () => {
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      await fireEvent.click(tagRows()[0]);
      expect(screen.getByRole('heading', { name: 'interviews' })).toBeInTheDocument();
      expect(screen.queryByText('Which tag survives the merge?')).toBeNull();

      await fireEvent.click(tagRows()[1], { ctrlKey: true });
      expect(screen.getByRole('heading', { name: '2 tags selected' })).toBeInTheDocument();
      expect(screen.getByText('Which tag survives the merge?')).toBeInTheDocument();
      expect(screen.queryByRole('heading', { name: 'interviews' })).toBeNull();
    });

    it('previews the delete impact before applying it', async () => {
      api.deleteTags.mockResolvedValue({ deleted_uuids: [UUID_A] });
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));
      await fireEvent.click(tagRows()[0]);

      await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
      await waitFor(() => expect(screen.getByText('500 files in total')).toBeInTheDocument());
      expect(api.getTagImpact).toHaveBeenCalledWith([UUID_A]);
      expect(api.deleteTags).not.toHaveBeenCalled();
      expect(screen.getByText('3 files you can see')).toBeInTheDocument();

      const deleteButtons = screen.getAllByRole('button', { name: 'Delete' });
      await fireEvent.click(deleteButtons[deleteButtons.length - 1]);
      await waitFor(() => expect(api.deleteTags).toHaveBeenCalledWith([UUID_A]));
    });

    it('previews removed vs retained associations before rejecting', async () => {
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));
      await fireEvent.click(tagRows()[1]);

      await fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
      await waitFor(() =>
        expect(screen.getByText('7 auto-applied links removed')).toBeInTheDocument()
      );
      expect(api.getTagReviewImpact).toHaveBeenCalledWith([UUID_B], 'reject');
      expect(screen.getByText('2 hand-applied links kept')).toBeInTheDocument();
      expect(api.rejectTags).not.toHaveBeenCalled();
    });
  });

  describe('filters', () => {
    it('narrows the list to the awaiting-review set', async () => {
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      api.listTags.mockResolvedValueOnce([allTags[1]]);
      await fireEvent.click(screen.getByRole('tab', { name: /Awaiting review/ }));

      await waitFor(() => expect(tagRows()).toHaveLength(1));
      expect(api.listTags).toHaveBeenLastCalledWith({
        awaiting_review: true,
        unused: undefined,
        scope: 'all',
      });
      expect(screen.getByText('roadmap')).toBeInTheDocument();
    });

    it('narrows the list to the unused set', async () => {
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      api.listTags.mockResolvedValueOnce([]);
      await fireEvent.click(screen.getByRole('tab', { name: /Unused/ }));

      await waitFor(() =>
        expect(api.listTags).toHaveBeenLastCalledWith({
          awaiting_review: undefined,
          unused: true,
          scope: 'all',
        })
      );
    });

    it('switches to the grouped collision endpoint for the collisions view', async () => {
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      api.listTagCollisions.mockResolvedValueOnce([
        {
          normalized_name: 'roadmap',
          members: [
            { uuid: UUID_A, name: 'Roadmap', source: 'manual', usage_count: 9, suggested_survivor: true },
            { uuid: UUID_B, name: 'roadmap', source: 'auto_ai', usage_count: 2, suggested_survivor: false },
          ],
          suggested_survivor_uuid: UUID_A,
          suggestions: [],
        },
      ]);
      await fireEvent.click(screen.getByRole('tab', { name: /Collisions/ }));

      await waitFor(() => expect(api.listTagCollisions).toHaveBeenCalledTimes(1));
      // One cluster header plus its two members.
      await waitFor(() => expect(tagRows()).toHaveLength(3));
      expect(screen.getByText('2 tags share this name')).toBeInTheDocument();
    });
  });

  describe('empty states', () => {
    const cases: Array<[string, RegExp, string]> = [
      ['All', /^All$/, 'No tags yet'],
      ['Awaiting review', /Awaiting review/, 'Nothing waiting on you'],
      ['Unused', /Unused/, 'No unused tags'],
      ['Collisions', /Collisions/, 'No duplicate tags'],
    ];

    it('renders a distinct empty state per filter', async () => {
      api.listTags.mockResolvedValue([]);
      api.listTagCollisions.mockResolvedValue([]);
      render(TagsPage);

      await waitFor(() => expect(screen.getByText('No tags yet')).toBeInTheDocument());

      for (const [, tabName, title] of cases.slice(1)) {
        await fireEvent.click(screen.getByRole('tab', { name: tabName }));
        await waitFor(() => expect(screen.getByText(title)).toBeInTheDocument());
      }

      // …and none of the four reuses another's copy.
      const titles = cases.map(([, , title]) => title);
      expect(new Set(titles).size).toBe(4);
    });
  });

  describe('ownership', () => {
    it('marks a shared tag in the list and leaves an owned one unmarked', async () => {
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      // `is_shared` is derived from ownership on the wire; the badge is the only
      // signal a user has that renaming this tag rewrites it for everyone.
      const [owned, shared] = tagRows();
      expect(within(owned).queryByText('Shared')).toBeNull();
      expect(within(shared).getByText('Shared')).toBeInTheDocument();
    });

    it('asks the backend for one ownership scope at a time', async () => {
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      api.listTags.mockResolvedValueOnce([allTags[0]]);
      await fireEvent.change(screen.getByLabelText('Tag ownership'), {
        target: { value: 'mine' },
      });

      await waitFor(() =>
        expect(api.listTags).toHaveBeenLastCalledWith({
          awaiting_review: undefined,
          unused: undefined,
          scope: 'mine',
        })
      );
    });

    it('promotes an owned tag to the shared vocabulary', async () => {
      api.promoteTags.mockResolvedValue({ impact: { tags: [], accessible_file_count: 0, total_file_count: 0 } });
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      await fireEvent.click(tagRows()[0]);
      await fireEvent.click(screen.getByRole('button', { name: 'Share with everyone' }));

      await waitFor(() => expect(api.promoteTags).toHaveBeenCalledWith([UUID_A]));
    });

    it('offers no promote control for an already-shared tag', async () => {
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      await fireEvent.click(tagRows()[1]);

      // Promoting a shared tag is a no-op, so the control is absent rather than
      // present-and-disabled.
      expect(screen.queryByRole('button', { name: 'Share with everyone' })).toBeNull();
    });

    it('hides the promote control from a non-admin', async () => {
      auth.role = 'user';
      render(TagsPage);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      await fireEvent.click(tagRows()[0]);

      expect(screen.queryByRole('button', { name: 'Share with everyone' })).toBeNull();
    });
  });
});
