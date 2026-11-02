import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/svelte';

/**
 * Coordinator-level tests. The modal owns every fetch, so the states that exist
 * only because of one (skeleton / error+retry / three distinct empty states) and
 * the detail-vs-bulk swap can only be exercised here.
 *
 * This was a route (`/tags`) until tags moved behind the gallery's Tags button:
 * tags are metadata over the library, not a destination, so they live in a modal
 * beside Collections. The assertions are unchanged by that move — which is the
 * point of keeping them.
 */
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      // Mirror i18next's plural resolution: a `count` option selects the
      // `_one` / `_other` variant. Without it, pluralized keys fall through to
      // the raw key and every assertion on that copy fails for the wrong reason.
      const count = opts?.count;
      const plural =
        typeof count === 'number' ? (count === 1 ? `${key}_one` : `${key}_other`) : undefined;
      let out = (plural && en[plural]) ?? en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

const api = vi.hoisted(() => ({
  createTag: vi.fn(),
  listFilesForTag: vi.fn(),
  listTags: vi.fn(),
  listTagCollisions: vi.fn(),
  getTagImpact: vi.fn(),
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
  return {
    user: readable({
      uuid: 'u1',
      email: 'a@example.com',
      get role() {
        return auth.role;
      },
    }),
  };
});

import TagManagerModal from './TagManagerModal.svelte';
import { capabilities } from '$stores/capabilities';
import { toastStore } from '$stores/toast';

/** The deployment's tenancy, as `GET /system/capabilities` reported it. */
function setTenancy(mode: 'single' | 'multi' | undefined) {
  capabilities.update((state) => ({ ...state, tenancyMode: mode }));
}

const UUID_A = 'aaaaaaaa-0000-0000-0000-000000000001';
const UUID_B = 'bbbbbbbb-0000-0000-0000-000000000002';

const allTags = [
  {
    uuid: UUID_A,
    name: 'interviews',
    source: 'manual',
    usage_count: 12,
    ownership: 'mine',
  },
  {
    uuid: UUID_B,
    name: 'roadmap',
    source: 'auto_ai',
    usage_count: 4,
    ownership: 'system',
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  auth.role = 'admin';
  setTenancy('single');
  api.listTags.mockResolvedValue(allTags);
  api.listTagCollisions.mockResolvedValue([]);
  api.listFilesForTag.mockResolvedValue({ files: [], total: 0 });
  api.getTagImpact.mockResolvedValue({
    tags: [],
    accessible_file_count: 3,
    total_file_count: 500,
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

describe('test doubles', () => {
  it('mocks only functions the tags API module really exports', async () => {
    // A mock of a deleted endpoint keeps a test green while asserting nothing.
    const actual = await vi.importActual<Record<string, unknown>>('$lib/api/tags');
    expect(Object.keys(api).filter((name) => typeof actual[name] !== 'function')).toEqual([]);
  });
});

describe('tag manager modal', () => {
  it('lists the tags the backend returned', async () => {
    render(TagManagerModal);
    await waitFor(() => expect(tagRows()).toHaveLength(2));
    expect(screen.getByText('interviews')).toBeInTheDocument();
    expect(screen.getByText('Used on 12 files')).toBeInTheDocument();
  });

  it('renders an error with retry — never the empty state — when the fetch fails', async () => {
    api.listTags.mockRejectedValueOnce({ response: { data: { detail: 'tag query exploded' } } });
    render(TagManagerModal);

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
      render(TagManagerModal);
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
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(2));
      await fireEvent.click(tagRows()[0]);

      await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
      await waitFor(() => expect(screen.getByText(/500 in total/)).toBeInTheDocument());
      expect(api.getTagImpact).toHaveBeenCalledWith([UUID_A]);
      expect(api.deleteTags).not.toHaveBeenCalled();
      expect(screen.getByText(/on 3 of your files/)).toBeInTheDocument();

      const deleteButtons = screen.getAllByRole('button', { name: 'Delete' });
      await fireEvent.click(deleteButtons[deleteButtons.length - 1]);
      await waitFor(() => expect(api.deleteTags).toHaveBeenCalledWith([UUID_A]));
    });
  });

  describe('filters', () => {
    it('narrows the list to the unused set', async () => {
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      api.listTags.mockResolvedValueOnce([]);
      await fireEvent.click(screen.getByRole('tab', { name: /Unused/ }));

      await waitFor(() =>
        expect(api.listTags).toHaveBeenLastCalledWith({
          unused: true,
          scope: 'all',
        })
      );
    });

    it('switches to the grouped collision endpoint for the collisions view', async () => {
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      api.listTagCollisions.mockResolvedValueOnce([
        {
          normalized_name: 'roadmap',
          members: [
            {
              uuid: UUID_A,
              name: 'Roadmap',
              source: 'manual',
              usage_count: 9,
              suggested_survivor: true,
            },
            {
              uuid: UUID_B,
              name: 'roadmap',
              source: 'auto_ai',
              usage_count: 2,
              suggested_survivor: false,
            },
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
      ['Unused', /Unused/, 'No unused tags'],
      ['Collisions', /Collisions/, 'No duplicate tags'],
    ];

    it('renders a distinct empty state per filter', async () => {
      api.listTags.mockResolvedValue([]);
      api.listTagCollisions.mockResolvedValue([]);
      render(TagManagerModal);

      await waitFor(() => expect(screen.getByText('No tags yet')).toBeInTheDocument());

      for (const [, tabName, title] of cases.slice(1)) {
        await fireEvent.click(screen.getByRole('tab', { name: tabName }));
        await waitFor(() => expect(screen.getByText(title)).toBeInTheDocument());
      }

      // …and none of the three reuses another's copy.
      const titles = cases.map(([, , title]) => title);
      expect(new Set(titles).size).toBe(cases.length);
    });
  });

  describe('ownership', () => {
    it('marks a shared tag in the list and leaves an owned one unmarked', async () => {
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      // `ownership` is derived per request on the wire; the badge is the only
      // signal a user has that renaming this tag rewrites it for everyone.
      const [owned, shared] = tagRows();
      expect(within(owned).queryByText('Shared')).toBeNull();
      expect(within(shared).getByText('Shared')).toBeInTheDocument();
    });

    it('asks the backend for one ownership scope at a time', async () => {
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      api.listTags.mockResolvedValueOnce([allTags[0]]);
      await fireEvent.change(screen.getByLabelText('Tag ownership'), {
        target: { value: 'mine' },
      });

      await waitFor(() =>
        expect(api.listTags).toHaveBeenLastCalledWith({
          unused: undefined,
          scope: 'mine',
        })
      );
    });

    it('promotes an owned tag to the shared vocabulary', async () => {
      api.promoteTags.mockResolvedValue({
        impact: { tags: [], accessible_file_count: 0, total_file_count: 0 },
      });
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      await fireEvent.click(tagRows()[0]);
      await fireEvent.click(screen.getByRole('button', { name: 'Share with everyone' }));

      await waitFor(() => expect(api.promoteTags).toHaveBeenCalledWith([UUID_A]));
    });

    it('offers no promote control for an already-shared tag', async () => {
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      await fireEvent.click(tagRows()[1]);

      // Promoting a shared tag is a no-op, so the control is absent rather than
      // present-and-disabled.
      expect(screen.queryByRole('button', { name: 'Share with everyone' })).toBeNull();
    });

    it('hides the promote control from a non-admin', async () => {
      auth.role = 'user';
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      await fireEvent.click(tagRows()[0]);

      expect(screen.queryByRole('button', { name: 'Share with everyone' })).toBeNull();
    });
  });

  describe('ownership gating', () => {
    it('offers no write controls for a tag shared with me', async () => {
      api.listTags.mockResolvedValue([
        { ...allTags[0], ownership: 'shared_with_me', name: 'their-tag' },
      ]);
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(1));

      await fireEvent.click(tagRows()[0]);

      // The backend answers 404 for these, so offering them would be a button
      // that can only fail. The badge explains the absence instead.
      expect(screen.queryByRole('button', { name: 'Rename' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
      // Both surfaces mark it: the list row badge tells you before you click,
      // the detail badge explains why the buttons are missing after you do.
      expect(screen.getAllByText('Shared with me')).toHaveLength(2);
    });

    it('offers write controls for a system tag only to an admin', async () => {
      api.listTags.mockResolvedValue([{ ...allTags[0], ownership: 'system', name: 'sys-tag' }]);
      auth.role = 'user';
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(1));

      await fireEvent.click(tagRows()[0]);

      expect(screen.queryByRole('button', { name: 'Rename' })).toBeNull();
    });

    it('offers write controls on my own tag', async () => {
      api.listTags.mockResolvedValue([{ ...allTags[0], ownership: 'mine' }]);
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(1));

      await fireEvent.click(tagRows()[0]);

      expect(screen.getByRole('button', { name: 'Rename' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
    });

    it('asks the backend for the scope named by the picker', async () => {
      render(TagManagerModal);
      await waitFor(() => expect(tagRows()).toHaveLength(2));

      await fireEvent.change(screen.getByLabelText('Tag ownership'), {
        target: { value: 'shared_with_me' },
      });

      // The picker's values ARE the ownership values the API reports, so this
      // asserts the two vocabularies have not drifted apart.
      await waitFor(() =>
        expect(api.listTags).toHaveBeenLastCalledWith({
          unused: undefined,
          scope: 'shared_with_me',
        })
      );
    });
  });
});

describe('create field', () => {
  it('holds at most the stored width, counted in characters rather than UTF-16 units', async () => {
    render(TagManagerModal);
    await waitFor(() => expect(tagRows()).toHaveLength(2));
    const field = screen.getByLabelText('New tag') as HTMLInputElement;

    await fireEvent.input(field, { target: { value: 'x'.repeat(60) } });
    expect(field.value).toBe('x'.repeat(50));

    const emoji = '\u{1F600}'.repeat(30);
    await fireEvent.input(field, { target: { value: emoji } });
    expect(field.value).toBe(emoji);
    expect(field.hasAttribute('maxlength')).toBe(false);
  });
});

describe('cluster view gating', () => {
  it('offers no write controls for a cluster member shared with me', async () => {
    api.listTagCollisions.mockResolvedValue([
      {
        normalized_name: 'roadmap',
        members: [
          {
            uuid: UUID_A,
            name: 'Roadmap',
            source: 'manual',
            usage_count: 9,
            suggested_survivor: true,
            ownership: 'mine',
          },
          {
            uuid: UUID_B,
            name: 'roadmap',
            source: 'manual',
            usage_count: 2,
            suggested_survivor: false,
            ownership: 'shared_with_me',
          },
        ],
        suggested_survivor_uuid: UUID_A,
        suggestions: [],
      },
    ]);
    render(TagManagerModal);
    await waitFor(() => expect(tagRows()).toHaveLength(2));
    await fireEvent.click(screen.getByRole('tab', { name: /Collisions/ }));
    await waitFor(() => expect(screen.getAllByRole('listbox')).toHaveLength(1));

    const rows = within(screen.getByRole('listbox', { name: 'roadmap' })).getAllByRole('option');
    await fireEvent.click(rows[2]);

    // Same tag, same gate as in the flat list: the server answers 404 here.
    expect(screen.getByRole('heading', { name: 'roadmap' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Rename' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Share…' })).toBeNull();
  });
});

describe('system tag write gate follows the deployment scope', () => {
  // The server writes a system tag only for `deployment_scope_writable`: any
  // admin in a single-tenant deployment, only a super_admin in a multi-tenant one.
  async function selectSystemTag() {
    api.listTags.mockResolvedValue([{ ...allTags[0], ownership: 'system', name: 'sys-tag' }]);
    render(TagManagerModal);
    await waitFor(() => expect(tagRows()).toHaveLength(1));
    await fireEvent.click(tagRows()[0]);
  }

  it('offers an admin of a single-tenant deployment the system tag writes', async () => {
    await selectSystemTag();
    expect(screen.getByRole('button', { name: 'Rename' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('offers an admin of a multi-tenant deployment no system tag writes', async () => {
    setTenancy('multi');
    await selectSystemTag();
    expect(screen.queryByRole('button', { name: 'Rename' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
  });

  it('offers a super_admin of a multi-tenant deployment the system tag writes', async () => {
    setTenancy('multi');
    auth.role = 'super_admin';
    await selectSystemTag();
    expect(screen.getByRole('button', { name: 'Rename' })).toBeInTheDocument();
  });

  it('offers a plain admin nothing while the tenancy is unknown', async () => {
    setTenancy(undefined);
    await selectSystemTag();
    expect(screen.queryByRole('button', { name: 'Rename' })).toBeNull();
  });

  it('hides promote from an admin of a multi-tenant deployment', async () => {
    setTenancy('multi');
    render(TagManagerModal);
    await waitFor(() => expect(tagRows()).toHaveLength(2));
    await fireEvent.click(tagRows()[0]);
    expect(screen.queryByRole('button', { name: 'Share with everyone' })).toBeNull();
  });
});

describe('search', () => {
  it('says the search matched nothing rather than that the library is empty', async () => {
    render(TagManagerModal);
    await waitFor(() => expect(tagRows()).toHaveLength(2));

    await fireEvent.input(screen.getByLabelText('Search tags'), { target: { value: 'zzz' } });

    expect(await screen.findByText('No results found for "zzz"')).toBeInTheDocument();
    expect(screen.queryByText('No tags yet')).toBeNull();
    expect(api.listTags).toHaveBeenCalledTimes(1);
  });
});

describe('create', () => {
  async function create(name: string) {
    const field = screen.getByLabelText('New tag');
    await fireEvent.input(field, { target: { value: name } });
    await fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(api.createTag).toHaveBeenCalledWith(name));
  }

  it('reports a name that matched an existing tag exactly as resolved, not created', async () => {
    const success = vi.spyOn(toastStore, 'success');
    api.createTag.mockResolvedValue({ uuid: UUID_A, name: 'interviews', ownership: 'mine' });
    render(TagManagerModal);
    await waitFor(() => expect(tagRows()).toHaveLength(2));

    await create('interviews');

    await waitFor(() =>
      expect(success).toHaveBeenCalledWith('That name resolved to the existing tag "interviews"')
    );
    expect(success).not.toHaveBeenCalledWith('Created "interviews"');
  });

  it('reports a new tag as created and selects it', async () => {
    const success = vi.spyOn(toastStore, 'success');
    const fresh = {
      uuid: 'fresh-uuid',
      name: 'fresh',
      source: 'manual',
      usage_count: 0,
      ownership: 'mine',
    };
    api.createTag.mockResolvedValue({ uuid: fresh.uuid, name: fresh.name, ownership: 'mine' });
    render(TagManagerModal);
    await waitFor(() => expect(tagRows()).toHaveLength(2));
    api.listTags.mockResolvedValue([...allTags, fresh]);

    await create('fresh');

    await waitFor(() => expect(success).toHaveBeenCalledWith('Created "fresh"'));
    expect(await screen.findByRole('heading', { name: 'fresh' })).toBeInTheDocument();
  });

  it('knows an existing tag the current view hides, and does not select it', async () => {
    const success = vi.spyOn(toastStore, 'success');
    api.createTag.mockResolvedValue({ uuid: UUID_A, name: 'interviews', ownership: 'mine' });
    render(TagManagerModal);
    await waitFor(() => expect(tagRows()).toHaveLength(2));
    // The unused view does not list a tag that is on files.
    api.listTags.mockImplementation(async (filters?: { unused?: boolean }) =>
      filters?.unused ? [allTags[1]] : allTags
    );
    await fireEvent.click(screen.getByRole('tab', { name: /Unused/ }));
    await waitFor(() => expect(tagRows()).toHaveLength(1));

    await create('interviews');

    await waitFor(() =>
      expect(success).toHaveBeenCalledWith('That name resolved to the existing tag "interviews"')
    );
    // Selecting a row the view cannot show would leave the detail pane blank
    // (and, below 768px, hide the list behind a "back" button).
    await waitFor(() => expect(tagRows()).toHaveLength(1));
    expect(screen.getByRole('status')).toHaveTextContent('No tags selected');
    expect(screen.queryByRole('button', { name: 'Back to tags' })).toBeNull();
  });
});
