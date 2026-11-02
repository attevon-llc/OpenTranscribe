import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return {
    t: readable((key: string, opts?: Record<string, unknown>) =>
      opts ? `${key} ${JSON.stringify(opts)}` : key
    ),
  };
});

const toast = vi.hoisted(() => ({
  show: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
  dismiss: vi.fn(),
  clear: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

const api = vi.hoisted(() => ({
  listTags: vi.fn(),
  listTagCollisions: vi.fn(),
  getTagImpact: vi.fn(),
  deleteTags: vi.fn(),
  mergeTags: vi.fn(),
  renameTag: vi.fn(),
  promoteTags: vi.fn(),
  createTag: vi.fn(),
  listFilesForTag: vi.fn(),
  listTagShares: vi.fn(),
  shareTag: vi.fn(),
  revokeTagShare: vi.fn(),
}));
vi.mock('$lib/api/tags', () => api);

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

/** The deployment's tenancy, as `GET /system/capabilities` reported it. */
function setTenancy(mode: 'single' | 'multi' | undefined) {
  capabilities.update((state) => ({ ...state, tenancyMode: mode }));
}

const A = 'aaaaaaaa-0000-0000-0000-000000000001';
const B = 'bbbbbbbb-0000-0000-0000-000000000002';
const C = 'cccccccc-0000-0000-0000-000000000003';

const tagA = {
  uuid: A,
  name: 'Zebra',
  source: 'manual',
  usage_count: 12,
  ownership: 'mine',
  can_write: true,
};
const tagB = {
  uuid: B,
  name: 'apple',
  source: 'auto_ai',
  usage_count: 4,
  ownership: 'mine',
  can_write: true,
};
const tagC = {
  uuid: C,
  name: 'Mango',
  source: 'manual',
  usage_count: 1,
  ownership: 'mine',
  can_write: true,
};
const library = [tagA, tagB, tagC];

const impact = { tags: [], accessible_file_count: 3, total_file_count: 500 };
const mutationResult = {
  tag: null,
  merged: false,
  requires_confirmation: false,
  deleted_uuids: [],
  impact,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** The listTags argument with undefined keys dropped, i.e. what reaches the wire. */
function listArgs(call: number) {
  return JSON.parse(JSON.stringify(api.listTags.mock.calls[call][0]));
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.role = 'admin';
  // 7d3ea6437: an admin gets system-tag writes only in a single-tenant deployment, and an
  // unreported tenancy reads as multi-tenant, so the fixture must say which deployment this is.
  setTenancy('single');
  api.listTags.mockResolvedValue(library);
  api.listTagCollisions.mockResolvedValue([]);
  api.getTagImpact.mockResolvedValue(impact);
  api.listFilesForTag.mockResolvedValue({ files: [], total: 0 });
  api.listTagShares.mockResolvedValue([]);
  api.deleteTags.mockResolvedValue(mutationResult);
  api.mergeTags.mockResolvedValue(mutationResult);
  api.renameTag.mockResolvedValue(mutationResult);
  api.promoteTags.mockResolvedValue(mutationResult);
});

function rows(): HTMLElement[] {
  const pane = document.querySelector('.list-pane') as HTMLElement;
  return within(pane).getAllByRole('option');
}
const rowNames = () => rows().map((row) => row.querySelector('.row-name')?.textContent);
const btn = (name: string) => screen.queryByRole('button', { name });

async function loaded(count = 3) {
  const view = render(TagManagerModal);
  await waitFor(() => expect(rows()).toHaveLength(count));
  return view;
}

describe('loading', () => {
  it('requests the plain list with only the scope, after showing a skeleton', async () => {
    const pending = deferred<typeof library>();
    api.listTags.mockReturnValueOnce(pending.promise);
    render(TagManagerModal);
    expect(document.querySelector('.list-skeleton')).not.toBeNull();
    expect(document.querySelector('.list-pane [role="option"]')).toBeNull();

    pending.resolve(library);
    await waitFor(() => expect(rows()).toHaveLength(3));
    expect(document.querySelector('.list-skeleton')).toBeNull();
    expect(api.listTags).toHaveBeenCalledTimes(1);
    expect(listArgs(0)).toEqual({ scope: 'all' });
  });

  it('asks for the unused view with unused:true and the scope', async () => {
    await loaded();
    await fireEvent.click(screen.getByRole('tab', { name: /tags\.manager\.filter\.unused/ }));
    await waitFor(() => expect(api.listTags).toHaveBeenCalledTimes(2));
    expect(listArgs(1)).toEqual({ unused: true, scope: 'all' });
  });

  it('combines the unused view with a chosen scope', async () => {
    await loaded();
    await fireEvent.change(screen.getByLabelText('tags.manager.scopeLabel'), {
      target: { value: 'mine' },
    });
    await waitFor(() => expect(api.listTags).toHaveBeenCalledTimes(2));
    await fireEvent.click(screen.getByRole('tab', { name: /tags\.manager\.filter\.unused/ }));
    await waitFor(() => expect(api.listTags).toHaveBeenCalledTimes(3));
    expect(listArgs(2)).toEqual({ unused: true, scope: 'mine' });
  });

  it('uses the collisions endpoint alone for that view and shows the clusters', async () => {
    api.listTagCollisions.mockResolvedValue([
      {
        normalized_name: 'zebra',
        members: [
          { ...tagA, suggested_survivor: false },
          { ...tagB, suggested_survivor: true },
        ],
        suggested_survivor_uuid: B,
        suggestions: [],
      },
    ]);
    await loaded();
    await fireEvent.click(screen.getByRole('tab', { name: /tags\.manager\.filter\.collisions/ }));
    await waitFor(() => expect(api.listTagCollisions).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(rows()).toHaveLength(3));
    expect(api.listTags).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('listbox', { name: 'zebra' })).toBeInTheDocument();
  });

  it('shows the server detail in an alert with retry, never an empty state', async () => {
    api.listTags.mockRejectedValueOnce({ response: { data: { detail: 'boom' } } });
    render(TagManagerModal);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('boom');
    expect(screen.queryByText('tags.manager.empty.allTitle')).toBeNull();
    expect(screen.queryByLabelText('tags.manager.searchLabel')).toBeNull();
    expect(document.querySelector('.list-pane [role="option"]')).toBeNull();

    await fireEvent.click(btn('tags.manager.retry')!);
    await waitFor(() => expect(rows()).toHaveLength(3));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('falls back to the load-failed key when the error carries no detail', async () => {
    api.listTags.mockRejectedValueOnce({});
    render(TagManagerModal);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('tags.manager.loadFailed');
  });

  it.each([
    ['all', /tags\.manager\.filter\.all$/, 'tags.manager.empty.allTitle'],
    ['unused', /tags\.manager\.filter\.unused/, 'tags.manager.empty.unusedTitle'],
    ['colliding', /tags\.manager\.filter\.collisions/, 'tags.manager.empty.collisionsTitle'],
  ])('shows its own empty state for the %s view', async (_view, tab, title) => {
    api.listTags.mockResolvedValue([]);
    api.listTagCollisions.mockResolvedValue([]);
    render(TagManagerModal);
    await waitFor(() =>
      expect(screen.getByText('tags.manager.empty.allTitle')).toBeInTheDocument()
    );
    if (title !== 'tags.manager.empty.allTitle') {
      await fireEvent.click(screen.getByRole('tab', { name: tab }));
    }
    await waitFor(() => expect(screen.getByText(title)).toBeInTheDocument());
    const shown = ['all', 'unused', 'collisions']
      .map((name) => `tags.manager.empty.${name}Title`)
      .filter((key) => screen.queryByText(key) !== null);
    expect(shown).toEqual([title]);
  });

  it('puts the row count on the active tab and none while loading', async () => {
    const pending = deferred<typeof library>();
    api.listTags.mockReturnValueOnce(pending.promise);
    render(TagManagerModal);
    expect(screen.getAllByRole('tab')[0].querySelector('.tab-badge')).toBeNull();
    pending.resolve(library);
    await waitFor(() =>
      expect(screen.getAllByRole('tab')[0].querySelector('.tab-badge')?.textContent).toBe('3')
    );
  });
});

describe('search and sort', () => {
  it('filters by a trimmed case-insensitive substring with no new request', async () => {
    await loaded();
    await fireEvent.input(screen.getByLabelText('tags.manager.searchLabel'), {
      target: { value: '  ZEB ' },
    });
    expect(rowNames()).toEqual(['Zebra']);
    expect(api.listTags).toHaveBeenCalledTimes(1);
    expect(screen.getAllByRole('tab')[0].querySelector('.tab-badge')?.textContent).toBe('1');
  });

  it('shows the clear button only with a query and empties the field with it', async () => {
    await loaded();
    expect(screen.queryByTitle('tags.manager.clearSearch')).toBeNull();
    const input = screen.getByLabelText('tags.manager.searchLabel') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'ap' } });
    await fireEvent.click(screen.getByTitle('tags.manager.clearSearch'));
    expect(input.value).toBe('');
    expect(rowNames()).toEqual(['Zebra', 'apple', 'Mango']);
    expect(screen.queryByTitle('tags.manager.clearSearch')).toBeNull();
  });

  it('keeps server order for usage and sorts by name with localeCompare', async () => {
    await loaded();
    expect(rowNames()).toEqual(['Zebra', 'apple', 'Mango']);

    const sort = document.querySelector('.sort-select') as HTMLSelectElement;
    expect(Array.from(sort.options).map((o) => o.value)).toEqual(['usage', 'name']);
    await fireEvent.change(sort, { target: { value: 'name' } });
    expect(rowNames()).toEqual(['apple', 'Mango', 'Zebra']);

    await fireEvent.change(sort, { target: { value: 'usage' } });
    expect(rowNames()).toEqual(['Zebra', 'apple', 'Mango']);
    expect(api.listTags).toHaveBeenCalledTimes(1);
  });

  it('hides the search and sort controls in the collisions view', async () => {
    api.listTagCollisions.mockResolvedValue([
      {
        normalized_name: 'zebra',
        members: [{ ...tagA, suggested_survivor: true }],
        suggested_survivor_uuid: A,
        suggestions: [],
      },
    ]);
    await loaded();
    expect(document.querySelector('.sort-select')).not.toBeNull();
    await fireEvent.click(screen.getByRole('tab', { name: /tags\.manager\.filter\.collisions/ }));
    await waitFor(() => expect(screen.getByRole('listbox', { name: 'zebra' })).toBeInTheDocument());
    expect(document.querySelector('.sort-select')).toBeNull();
    expect(screen.queryByLabelText('tags.manager.searchLabel')).toBeNull();
  });
});

describe('create', () => {
  const field = () => screen.getByLabelText('tags.manager.create.label') as HTMLInputElement;
  const submit = () =>
    screen.getByRole('button', { name: /tags\.manager\.create\.(submit|creating)/ });

  it('has a 50 character field and a disabled button until there is a non-blank name', async () => {
    await loaded();
    // D16 (8f96d6e10): clamped to 50 code points by clampTagNameInput, not a native maxlength
    // (which counts UTF-16 units).
    expect(field()).not.toHaveAttribute('maxlength');
    expect(field()).toHaveAttribute('placeholder', 'tags.manager.create.placeholder');
    await fireEvent.input(field(), { target: { value: 'x'.repeat(60) } });
    expect(field().value).toBe('x'.repeat(50));
    await fireEvent.input(field(), { target: { value: '\u{1F600}'.repeat(60) } });
    expect(Array.from(field().value)).toHaveLength(50);
    await fireEvent.input(field(), { target: { value: '' } });
    expect(submit()).toBeDisabled();
    await fireEvent.input(field(), { target: { value: '   ' } });
    expect(submit()).toBeDisabled();
    await fireEvent.input(field(), { target: { value: ' x ' } });
    expect(submit()).toBeEnabled();
  });

  it('sends the trimmed name, clears the field, reloads and selects the returned tag', async () => {
    const fresh = {
      uuid: 'new-1',
      name: 'Fresh',
      source: 'manual',
      usage_count: 0,
      ownership: 'mine',
      can_write: true,
    };
    api.createTag.mockResolvedValue(fresh);
    await loaded();
    api.listTags.mockResolvedValue([...library, fresh]);

    await fireEvent.input(field(), { target: { value: '  Fresh ' } });
    await fireEvent.click(submit());

    await waitFor(() => expect(api.createTag).toHaveBeenCalledWith('Fresh'));
    expect(toast.success).toHaveBeenCalledWith('tags.manager.create.created {"name":"Fresh"}');
    await waitFor(() => expect(field().value).toBe(''));
    await waitFor(() => expect(rows()).toHaveLength(4));
    expect(api.listTags).toHaveBeenCalledTimes(2);
    expect(await screen.findByRole('heading', { level: 2, name: 'Fresh' })).toBeInTheDocument();
  });

  it('reports the existing tag when the returned name differs from the one typed', async () => {
    api.createTag.mockResolvedValue(tagB);
    await loaded();
    await fireEvent.input(field(), { target: { value: 'APPLE' } });
    await fireEvent.click(submit());
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('tags.manager.create.resolved {"name":"apple"}')
    );
    expect(await screen.findByRole('heading', { level: 2, name: 'apple' })).toBeInTheDocument();
  });

  it('submits once on Enter, and not while the request runs', async () => {
    const pending = deferred<typeof tagB>();
    api.createTag.mockReturnValue(pending.promise);
    await loaded();
    await fireEvent.input(field(), { target: { value: 'apple2' } });
    await fireEvent.keyDown(field(), { key: 'Enter' });
    await waitFor(() => expect(api.createTag).toHaveBeenCalledTimes(1));
    expect(submit()).toHaveTextContent('tags.manager.create.creating');
    expect(submit()).toBeDisabled();
    expect(field()).toBeDisabled();

    await fireEvent.keyDown(field(), { key: 'Enter' });
    expect(api.createTag).toHaveBeenCalledTimes(1);
    pending.resolve({ ...tagB, name: 'apple2' });
    await waitFor(() => expect(submit()).toHaveTextContent('tags.manager.create.submit'));
  });

  it('shows the server detail and keeps the text when creating fails', async () => {
    api.createTag.mockRejectedValue({ response: { data: { detail: 'Tag name is required' } } });
    await loaded();
    await fireEvent.input(field(), { target: { value: 'keep me' } });
    await fireEvent.click(submit());
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Tag name is required'));
    expect(field().value).toBe('keep me');
    expect(api.listTags).toHaveBeenCalledTimes(1);
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('falls back to the create-failed key', async () => {
    api.createTag.mockRejectedValue({});
    await loaded();
    await fireEvent.input(field(), { target: { value: 'n' } });
    await fireEvent.click(submit());
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('tags.manager.create.failed'));
  });
});

describe('selection', () => {
  it('shows the prompt with nothing selected, the detail for one, the summary for several', async () => {
    await loaded();
    expect(document.querySelector('.select-prompt')).toHaveTextContent('tags.manager.selectPrompt');
    expect(btn('tags.manager.back')).toBeNull();

    await fireEvent.click(rows()[0]);
    expect(screen.getByRole('heading', { level: 2, name: 'Zebra' })).toBeInTheDocument();
    expect(document.querySelector('.select-prompt')).toBeNull();

    await fireEvent.click(rows()[1], { ctrlKey: true });
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(
      'tags.manager.bulk.title {"count":2}'
    );
  });

  it('toggles a row off with ctrl+click, falling back to the remaining detail', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(rows()[1], { ctrlKey: true });
    await fireEvent.click(rows()[0], { ctrlKey: true });
    expect(screen.getByRole('heading', { level: 2, name: 'apple' })).toBeInTheDocument();
  });

  it('selects a shift range', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(rows()[2], { shiftKey: true });
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('"count":3');
  });

  it('the back button clears the selection', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.back')!);
    expect(document.querySelector('.select-prompt')).not.toBeNull();
    expect(btn('tags.manager.back')).toBeNull();
  });

  it('the summary clear button clears the selection', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(rows()[1], { ctrlKey: true });
    await fireEvent.click(btn('tags.manager.action.clearSelection')!);
    expect(document.querySelector('.select-prompt')).not.toBeNull();
  });

  it('clears the selection and any preview when the scope or the view changes', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.change(screen.getByLabelText('tags.manager.scopeLabel'), {
      target: { value: 'system' },
    });
    await waitFor(() => expect(document.querySelector('.select-prompt')).not.toBeNull());
    expect(listArgs(1)).toEqual({ scope: 'system' });

    await waitFor(() => expect(rows()).toHaveLength(3));
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.action.delete')!);
    await waitFor(() => expect(api.getTagImpact).toHaveBeenCalled());
    await fireEvent.click(screen.getByRole('tab', { name: /tags\.manager\.filter\.unused/ }));
    await waitFor(() => expect(document.querySelector('.select-prompt')).not.toBeNull());
    expect(document.querySelector('.modal-delete-button')).toBeNull();
  });

  it('selects every member from a collision header and preselects the suggested survivor', async () => {
    api.listTagCollisions.mockResolvedValue([
      {
        normalized_name: 'zebra',
        members: [
          { ...tagA, suggested_survivor: false },
          { ...tagC, suggested_survivor: true },
        ],
        suggested_survivor_uuid: C,
        suggestions: [],
      },
    ]);
    await loaded();
    await fireEvent.click(screen.getByRole('tab', { name: /tags\.manager\.filter\.collisions/ }));
    await waitFor(() => expect(screen.getByRole('listbox', { name: 'zebra' })).toBeInTheDocument());

    await fireEvent.click(rows()[0]);
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('"count":2');
    const checked = (screen.getAllByRole('radio') as HTMLInputElement[]).find((r) => r.checked);
    expect(checked?.value).toBe(C);

    await fireEvent.click(rows()[0]);
    expect(document.querySelector('.select-prompt')).not.toBeNull();
  });

  it('adds a near-match suggestion to the selection', async () => {
    api.listTagCollisions.mockResolvedValue([
      {
        normalized_name: 'zebra',
        members: [{ ...tagA, suggested_survivor: true }],
        suggested_survivor_uuid: A,
        suggestions: [{ ...tagB, similarity: 0.9 }],
      },
    ]);
    await loaded();
    await fireEvent.click(screen.getByRole('tab', { name: /tags\.manager\.filter\.collisions/ }));
    await waitFor(() => expect(rows()).toHaveLength(2));
    await fireEvent.click(rows()[1]);
    await fireEvent.click(screen.getByRole('button', { name: /alsoSimilar/ }));
    await fireEvent.click(screen.getByRole('button', { name: /apple/ }));
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('"count":2');
  });
});

describe('files of the selected tag', () => {
  it('fetches the files of a single selection and lists them, but not for several', async () => {
    api.listFilesForTag.mockResolvedValue({
      files: [{ uuid: 'f1', display_title: 'Call one', formatted_duration: '1:00' }],
      total: 1,
    });
    await loaded();
    await fireEvent.click(rows()[0]);
    await waitFor(() => expect(api.listFilesForTag).toHaveBeenCalledWith(A));
    expect(await screen.findByRole('link', { name: /Call one/ })).toHaveAttribute(
      'href',
      '/files/f1'
    );
    expect(document.querySelector('.touches-list')).not.toBeNull();

    api.listFilesForTag.mockClear();
    await fireEvent.click(rows()[1], { ctrlKey: true });
    expect(api.listFilesForTag).not.toHaveBeenCalled();
  });

  it('discards a slow answer for an earlier selection', async () => {
    const slow = deferred<{ files: unknown[]; total: number }>();
    api.listFilesForTag.mockImplementation((uuid: string) =>
      uuid === A
        ? slow.promise
        : Promise.resolve({
            files: [{ uuid: 'fb', display_title: 'B file', formatted_duration: null }],
            total: 1,
          })
    );
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(rows()[1]);
    expect(await screen.findByRole('link', { name: /B file/ })).toBeInTheDocument();

    slow.resolve({
      files: [{ uuid: 'fa', display_title: 'A file', formatted_duration: null }],
      total: 1,
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole('link', { name: /A file/ })).toBeNull();
    expect(screen.getByRole('link', { name: /B file/ })).toBeInTheDocument();
  });

  it('treats a failed fetch as no files, without a toast', async () => {
    api.listFilesForTag.mockRejectedValue(new Error('x'));
    await loaded();
    await fireEvent.click(rows()[0]);
    expect(await screen.findByText('tags.manager.detail.noFiles')).toBeInTheDocument();
    expect(toast.error).not.toHaveBeenCalled();
  });
});

describe('rename', () => {
  async function renameTo(name: string) {
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.detail.rename')!);
    await fireEvent.input(screen.getByLabelText('tags.manager.detail.renameLabel'), {
      target: { value: name },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.detail.rename' }));
  }

  it('sends the new name with confirm_merge false, toasts, clears the selection and reloads', async () => {
    await loaded();
    await renameTo('Zebras');
    await waitFor(() =>
      expect(api.renameTag).toHaveBeenCalledWith(A, { name: 'Zebras', confirm_merge: false })
    );
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('tags.manager.toast.renamed'));
    await waitFor(() => expect(document.querySelector('.select-prompt')).not.toBeNull());
    expect(api.listTags).toHaveBeenCalledTimes(2);
  });

  it('toasts the merged key when the server reports a merge', async () => {
    api.renameTag.mockResolvedValue({ ...mutationResult, merged: true });
    await loaded();
    await renameTo('apple');
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('tags.manager.toast.merged'));
  });

  it('shows the warning, keeps the selection and applies nothing when confirmation is required', async () => {
    api.renameTag.mockResolvedValue({ ...mutationResult, requires_confirmation: true });
    await loaded();
    await renameTo('apple');
    expect(
      await screen.findByRole('heading', {
        level: 3,
        name: 'tags.manager.detail.mergeOnRenameTitle',
      })
    ).toBeInTheDocument();
    expect(screen.getByText('tags.manager.impact.total {"count":500}')).toBeInTheDocument();
    expect(toast.success).not.toHaveBeenCalled();
    expect(api.listTags).toHaveBeenCalledTimes(1);
    expect(document.querySelector('.select-prompt')).toBeNull();
  });

  it('confirming repeats the call with confirm_merge true; keep editing drops the warning', async () => {
    api.renameTag.mockResolvedValueOnce({ ...mutationResult, requires_confirmation: true });
    await loaded();
    await renameTo('apple');
    await fireEvent.click(
      await screen.findByRole('button', { name: 'tags.manager.detail.keepEditing' })
    );
    expect(screen.queryByText('tags.manager.detail.mergeOnRenameTitle')).toBeNull();

    api.renameTag.mockResolvedValueOnce({ ...mutationResult, requires_confirmation: true });
    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.detail.rename' }));
    await fireEvent.click(
      await screen.findByRole('button', { name: 'tags.manager.detail.confirmMergeRename' })
    );
    await waitFor(() =>
      expect(api.renameTag).toHaveBeenLastCalledWith(A, { name: 'apple', confirm_merge: true })
    );
    expect(api.renameTag).toHaveBeenCalledTimes(3);
  });

  it('keeps the selection and shows the server detail when renaming fails', async () => {
    api.renameTag.mockRejectedValue({ response: { data: { detail: 'Tag not found' } } });
    await loaded();
    await renameTo('Zebras');
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Tag not found'));
    expect(screen.getByLabelText('tags.manager.detail.renameLabel')).toBeInTheDocument();
    expect(api.listTags).toHaveBeenCalledTimes(1);
  });
});

describe('delete, merge, promote', () => {
  it('previews a single delete, then deletes, toasts, clears and reloads', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.action.delete')!);
    await waitFor(() => expect(api.getTagImpact).toHaveBeenCalledWith([A]));
    expect(api.deleteTags).not.toHaveBeenCalled();

    await fireEvent.click(
      await waitFor(() => {
        const confirm = document.querySelector('.modal-delete-button') as HTMLElement;
        expect(confirm).not.toBeNull();
        return confirm;
      })
    );
    await waitFor(() => expect(api.deleteTags).toHaveBeenCalledWith([A]));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('tags.manager.toast.deleted'));
    await waitFor(() => expect(document.querySelector('.select-prompt')).not.toBeNull());
    expect(api.listTags).toHaveBeenCalledTimes(2);
  });

  it('reopens the single delete dialog after Cancel and still deletes on confirm', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.action.delete')!);
    await waitFor(() => expect(document.querySelector('.modal-cancel-button')).not.toBeNull());
    await fireEvent.click(document.querySelector('.modal-cancel-button') as HTMLElement);
    await waitFor(() => expect(document.querySelector('.modal-cancel-button')).toBeNull());
    expect(api.deleteTags).not.toHaveBeenCalled();

    await fireEvent.click(btn('tags.manager.action.delete')!);
    await waitFor(() => expect(api.getTagImpact).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(document.querySelector('.modal-delete-button')).not.toBeNull());
    await fireEvent.click(document.querySelector('.modal-delete-button') as HTMLElement);
    await waitFor(() => expect(api.deleteTags).toHaveBeenCalledWith([A]));
  });

  it('calls deleteTags once when a single delete is confirmed twice while it runs', async () => {
    const pending = deferred<typeof mutationResult>();
    api.deleteTags.mockReturnValueOnce(pending.promise);
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.action.delete')!);
    await waitFor(() => expect(document.querySelector('.modal-delete-button')).not.toBeNull());
    const confirm = document.querySelector('.modal-delete-button') as HTMLElement;
    await fireEvent.click(confirm);
    await fireEvent.click(confirm);
    await waitFor(() => expect(api.deleteTags).toHaveBeenCalledTimes(1));
    pending.resolve(mutationResult);
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(api.deleteTags).toHaveBeenCalledTimes(1);
  });

  it('keeps the selection and the preview when a delete fails', async () => {
    api.deleteTags.mockRejectedValue({ response: { data: { detail: 'nope' } } });
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.action.delete')!);
    await waitFor(() => expect(document.querySelector('.modal-delete-button')).not.toBeNull());
    await fireEvent.click(document.querySelector('.modal-delete-button') as HTMLElement);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('nope'));
    expect(screen.getByRole('heading', { level: 2, name: 'Zebra' })).toBeInTheDocument();
    expect(api.listTags).toHaveBeenCalledTimes(1);
  });

  it('falls back to the preview-failed key when the impact request fails', async () => {
    api.getTagImpact.mockRejectedValue({});
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.action.delete')!);
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('tags.manager.toast.previewFailed')
    );
    expect(api.deleteTags).not.toHaveBeenCalled();
  });

  // D20 (0749fb73a): the bulk delete preview is the shared confirmation dialog, naming every
  // selected tag and carrying both counts, not an inline panel with a second Delete button.
  const bulkMessage =
    'tags.manager.impact.deleteMessage {"name":"Zebra, Mango","accessible":3,"total":500}';

  async function previewBulkDelete() {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(rows()[2], { ctrlKey: true });
    await fireEvent.click(btn('tags.manager.action.delete')!);
    await waitFor(() => expect(api.getTagImpact).toHaveBeenCalledWith([A, C]));
    expect(await screen.findByText(bulkMessage)).toBeInTheDocument();
  }

  it('previews a bulk delete over every selected tag and deletes them all', async () => {
    await previewBulkDelete();
    expect(api.deleteTags).not.toHaveBeenCalled();
    await fireEvent.click(document.querySelector('.modal-delete-button') as HTMLElement);
    await waitFor(() => expect(api.deleteTags).toHaveBeenCalledWith([A, C]));
  });

  it('cancels a bulk delete preview without deleting anything', async () => {
    await previewBulkDelete();
    await fireEvent.click(document.querySelector('.modal-cancel-button') as HTMLElement);
    await waitFor(() => expect(screen.queryByText(bulkMessage)).toBeNull());
    expect(api.deleteTags).not.toHaveBeenCalled();
  });

  it('reopens the bulk delete dialog after Cancel and still deletes on confirm', async () => {
    await previewBulkDelete();
    await fireEvent.click(document.querySelector('.modal-cancel-button') as HTMLElement);
    await waitFor(() => expect(screen.queryByText(bulkMessage)).toBeNull());

    await fireEvent.click(btn('tags.manager.action.delete')!);
    await waitFor(() => expect(api.getTagImpact).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(bulkMessage)).toBeInTheDocument();
    await fireEvent.click(document.querySelector('.modal-delete-button') as HTMLElement);
    await waitFor(() => expect(api.deleteTags).toHaveBeenCalledWith([A, C]));
  });

  it('calls deleteTags once when the confirm is clicked twice while it runs', async () => {
    const pending = deferred<typeof mutationResult>();
    api.deleteTags.mockReturnValueOnce(pending.promise);
    await previewBulkDelete();
    const confirm = document.querySelector('.modal-delete-button') as HTMLElement;
    await fireEvent.click(confirm);
    await fireEvent.click(confirm);
    await waitFor(() => expect(api.deleteTags).toHaveBeenCalledTimes(1));
    pending.resolve(mutationResult);
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(api.deleteTags).toHaveBeenCalledTimes(1);
  });

  it('previews a merge over the non-survivors and merges into the survivor', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(rows()[1], { ctrlKey: true });
    // Highest usage (Zebra, A) is the preselected survivor.
    await fireEvent.click(btn('tags.manager.action.merge')!);
    await waitFor(() => expect(api.getTagImpact).toHaveBeenCalledWith([B]));
    expect(api.mergeTags).not.toHaveBeenCalled();

    await fireEvent.click(screen.getAllByRole('button', { name: 'tags.manager.action.merge' })[1]);
    await waitFor(() => expect(api.mergeTags).toHaveBeenCalledWith(A, [B]));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('tags.manager.toast.merged'));
    await waitFor(() => expect(document.querySelector('.select-prompt')).not.toBeNull());
  });

  it('promotes a single and a bulk selection with the selected uuids', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.action.promote')!);
    await waitFor(() => expect(api.promoteTags).toHaveBeenCalledWith([A]));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('tags.manager.promote.done'));

    await waitFor(() => expect(rows()).toHaveLength(3));
    await fireEvent.click(rows()[0]);
    await fireEvent.click(rows()[1], { ctrlKey: true });
    await fireEvent.click(btn('tags.manager.action.promote')!);
    await waitFor(() => expect(api.promoteTags).toHaveBeenLastCalledWith([A, B]));
  });

  it('shows the failure detail and keeps the selection when promoting fails', async () => {
    api.promoteTags.mockRejectedValue({ response: { data: { detail: 'not allowed' } } });
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.action.promote')!);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('not allowed'));
    expect(screen.getByRole('heading', { level: 2, name: 'Zebra' })).toBeInTheDocument();
  });

  it('ignores clicks on the rows of a mutation in flight and relabels the busy control', async () => {
    const pending = deferred<typeof mutationResult>();
    api.promoteTags.mockReturnValue(pending.promise);
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.action.promote')!);
    expect(
      await screen.findByRole('button', { name: 'tags.manager.action.promoting' })
    ).toBeDisabled();

    // Ctrl+click would toggle the selected row off if it were interactive.
    await fireEvent.click(rows()[0], { ctrlKey: true });
    expect(screen.getByRole('heading', { level: 2, name: 'Zebra' })).toBeInTheDocument();

    pending.resolve(mutationResult);
    await waitFor(() => expect(document.querySelector('.select-prompt')).not.toBeNull());
  });
});

describe('who sees which write controls', () => {
  const system = { ...tagA, ownership: 'system', can_write: true };

  // 7d3ea6437: system-tag writes follow the deployment scope. The beforeEach fixture is a
  // single-tenant deployment; the multi-tenant and unreported cases are pinned below.
  it('gives an admin Rename and Delete, but not Share or Promote, on a system tag of a single-tenant deployment', async () => {
    api.listTags.mockResolvedValue([system]);
    await loaded(1);
    await fireEvent.click(rows()[0]);
    expect(btn('tags.manager.detail.rename')).not.toBeNull();
    expect(btn('tags.manager.action.delete')).not.toBeNull();
    expect(btn('tags.manager.share.button')).toBeNull();
    expect(btn('tags.manager.action.promote')).toBeNull();
  });

  it.each([['multi' as const], [undefined]])(
    'gives an admin nothing writable on a system tag when the tenancy is %s',
    async (mode) => {
      setTenancy(mode);
      // The server withholds the write (can_write false); promote stays gated by tenancy.
      api.listTags.mockResolvedValue([{ ...system, can_write: false }]);
      await loaded(1);
      await fireEvent.click(rows()[0]);
      expect(btn('tags.manager.detail.rename')).toBeNull();
      expect(btn('tags.manager.action.delete')).toBeNull();
      expect(btn('tags.manager.share.button')).toBeNull();
      expect(btn('tags.manager.action.promote')).toBeNull();
    }
  );

  it('still gives a super_admin the system tag writes in a multi-tenant deployment', async () => {
    setTenancy('multi');
    auth.role = 'super_admin';
    api.listTags.mockResolvedValue([system]);
    await loaded(1);
    await fireEvent.click(rows()[0]);
    expect(btn('tags.manager.detail.rename')).not.toBeNull();
    expect(btn('tags.manager.action.delete')).not.toBeNull();
  });

  it('treats a super_admin as an admin', async () => {
    auth.role = 'super_admin';
    api.listTags.mockResolvedValue([system]);
    await loaded(1);
    await fireEvent.click(rows()[0]);
    expect(btn('tags.manager.action.delete')).not.toBeNull();
  });

  it('gives a plain user nothing writable on a system tag and no promote on their own', async () => {
    auth.role = 'user';
    api.listTags.mockResolvedValue([{ ...system, can_write: false }, tagB]);
    await loaded(2);
    await fireEvent.click(rows()[0]);
    expect(btn('tags.manager.detail.rename')).toBeNull();
    expect(btn('tags.manager.action.delete')).toBeNull();

    await fireEvent.click(rows()[1]);
    expect(btn('tags.manager.detail.rename')).not.toBeNull();
    expect(btn('tags.manager.share.button')).not.toBeNull();
    expect(btn('tags.manager.action.promote')).toBeNull();
  });
});

describe('sharing', () => {
  it('opens the share dialog for the selected tag', async () => {
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.share.button')!);
    await waitFor(() => expect(api.listTagShares).toHaveBeenCalledWith(A));
    expect(document.querySelector('.tag-share')).not.toBeNull();
  });

  it('reloads the list when the dialog reports a change', async () => {
    api.listTagShares.mockResolvedValue([{ uuid: 's1', target_type: 'user', display_name: 'Pat' }]);
    api.revokeTagShare.mockResolvedValue(undefined);
    await loaded();
    await fireEvent.click(rows()[0]);
    await fireEvent.click(btn('tags.manager.share.button')!);
    const revoke = await screen.findByTitle('tags.manager.share.revokeTooltip');
    api.listTags.mockClear();

    await fireEvent.click(revoke);
    await waitFor(() => expect(api.revokeTagShare).toHaveBeenCalledWith(A, 's1'));
    await waitFor(() => expect(api.listTags).toHaveBeenCalledTimes(1));
  });
});
