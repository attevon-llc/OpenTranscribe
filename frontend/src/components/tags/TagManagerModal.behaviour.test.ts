import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyWithOptionsLocale()
);

const notify = vi.hoisted(() => ({
  show: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
  dismiss: vi.fn(),
  clear: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: notify }));

const tagsApi = vi.hoisted(() => ({
  listTags: vi.fn(),
  listTagCollisions: vi.fn(),
  createTag: vi.fn(),
  getTagImpact: vi.fn(),
  deleteTags: vi.fn(),
  mergeTags: vi.fn(),
  renameTag: vi.fn(),
  promoteTags: vi.fn(),
  listFilesForTag: vi.fn(),
  listTagShares: vi.fn(),
  shareTag: vi.fn(),
  revokeTagShare: vi.fn(),
}));
vi.mock('$lib/api/tags', () => tagsApi);

const caller = vi.hoisted(() => ({ role: 'user' }));
vi.mock('$stores/auth', async () => {
  const { readable } = await import('svelte/store');
  return {
    user: readable({
      uuid: 'caller',
      email: 'caller@example.com',
      get role() {
        return caller.role;
      },
    }),
  };
});

import TagManagerModal from './TagManagerModal.svelte';
import { capabilities } from '$stores/capabilities';

const K1 = 'a1000000-0000-4000-8000-0000000000a1';
const K2 = 'a2000000-0000-4000-8000-0000000000a2';
const K3 = 'a3000000-0000-4000-8000-0000000000a3';
const K4 = 'a4000000-0000-4000-8000-0000000000a4';

const standup = {
  uuid: K1,
  name: 'standup',
  source: 'manual',
  usage_count: 7,
  ownership: 'mine',
  can_write: true,
};
const retro = { ...standup, uuid: K2, name: 'retro', usage_count: 3 };
const planning = { ...standup, uuid: K3, name: 'planning', usage_count: 1 };
const visible = [standup, retro, planning];

const outcome = {
  tag: null,
  merged: false,
  requires_confirmation: false,
  deleted_uuids: [],
  impact: { tags: [], accessible_file_count: 1, total_file_count: 1 },
};

function setTenancy(mode: 'single' | 'multi' | undefined) {
  capabilities.update((state) => ({ ...state, tenancyMode: mode }));
}

beforeEach(() => {
  vi.clearAllMocks();
  caller.role = 'user';
  setTenancy('single');
  tagsApi.listTags.mockResolvedValue(visible);
  tagsApi.listTagCollisions.mockResolvedValue([]);
  tagsApi.listFilesForTag.mockResolvedValue({ files: [], total: 0 });
  tagsApi.listTagShares.mockResolvedValue([]);
  tagsApi.getTagImpact.mockResolvedValue(outcome.impact);
  tagsApi.deleteTags.mockResolvedValue(outcome);
  tagsApi.promoteTags.mockResolvedValue(outcome);
});

const listRows = () =>
  within(document.querySelector('.list-pane') as HTMLElement).queryAllByRole('option');
const button = (name: string) => screen.queryByRole('button', { name });
const createField = () => screen.getByLabelText('tags.manager.create.label') as HTMLInputElement;
const createButton = () => screen.getByRole('button', { name: 'tags.manager.create.submit' });
const scopeSelect = () => screen.getByLabelText('tags.manager.scopeLabel') as HTMLSelectElement;
const tab = (key: string) =>
  screen.getByRole('tab', { name: new RegExp(`tags\\.manager\\.filter\\.${key}`) });

async function mounted(rows = 3) {
  const view = render(TagManagerModal);
  await waitFor(() => expect(listRows()).toHaveLength(rows));
  return view;
}

async function create(name: string) {
  await fireEvent.input(createField(), { target: { value: name } });
  await fireEvent.click(createButton());
}

describe('create reports an existing tag as resolved', () => {
  it('says resolved for an exact-spelling hit on a tag the view already lists', async () => {
    tagsApi.createTag.mockResolvedValue(retro);
    await mounted();
    await create('retro');
    await waitFor(() =>
      expect(notify.success).toHaveBeenCalledWith('tags.manager.create.resolved {"name":"retro"}')
    );
    // The all-tags view knows every visible tag already: no extra lookup.
    expect(tagsApi.listTags).toHaveBeenCalledTimes(2);
  });

  it('asks the unfiltered list first when another scope is shown', async () => {
    tagsApi.listTags.mockImplementation(async (filters?: { scope?: string }) =>
      filters?.scope === 'system' ? [] : visible
    );
    tagsApi.createTag.mockResolvedValue(standup);
    render(TagManagerModal);
    await waitFor(() => expect(tagsApi.listTags).toHaveBeenCalledTimes(1));
    await fireEvent.change(scopeSelect(), { target: { value: 'system' } });
    await waitFor(() =>
      expect(screen.getByText('tags.manager.empty.allTitle')).toBeInTheDocument()
    );
    tagsApi.listTags.mockClear();

    await create('standup');
    await waitFor(() =>
      expect(notify.success).toHaveBeenCalledWith('tags.manager.create.resolved {"name":"standup"}')
    );
    expect(tagsApi.listTags.mock.calls[0]).toEqual([]);
    expect(tagsApi.listTags.mock.invocationCallOrder[0]).toBeLessThan(
      tagsApi.createTag.mock.invocationCallOrder[0]
    );
  });

  it('falls back to its own rows when that lookup fails, and still creates', async () => {
    const fresh = { ...standup, uuid: K4, name: 'demo day', usage_count: 0 };
    let created = false;
    tagsApi.listTags.mockImplementation(async (filters?: { unused?: boolean }) => {
      if (filters === undefined) throw new Error('lookup down');
      if (!filters.unused) return visible;
      return created ? [planning, fresh] : [planning];
    });
    tagsApi.createTag.mockImplementation(async () => {
      created = true;
      return fresh;
    });
    await mounted();
    await fireEvent.click(tab('unused'));
    await waitFor(() => expect(listRows()).toHaveLength(1));

    await create('demo day');
    await waitFor(() =>
      expect(notify.success).toHaveBeenCalledWith('tags.manager.create.created {"name":"demo day"}')
    );
    expect(notify.error).not.toHaveBeenCalled();
    expect(await screen.findByRole('heading', { level: 2, name: 'demo day' })).toBeInTheDocument();
  });

  it('selects nothing when the reloaded view does not contain the returned tag', async () => {
    const elsewhere = { ...standup, uuid: K4, name: 'offsite', ownership: 'system' };
    tagsApi.createTag.mockResolvedValue(elsewhere);
    await mounted();
    await fireEvent.change(scopeSelect(), { target: { value: 'mine' } });
    await waitFor(() => expect(tagsApi.listTags).toHaveBeenCalledTimes(2));

    await create('offsite');
    await waitFor(() => expect(notify.success).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(createField().value).toBe(''));
    expect(document.querySelector('.select-prompt')).not.toBeNull();
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull();
  });
});

describe('a search that matches nothing', () => {
  it('shows the no-results state for the trimmed query, not the empty-library one', async () => {
    await mounted();
    await fireEvent.input(screen.getByLabelText('tags.manager.searchLabel'), {
      target: { value: '  zzz ' },
    });
    expect(screen.getByText('tags.manager.empty.noMatchTitle {"query":"zzz"}')).toBeInTheDocument();
    expect(screen.queryByText('tags.manager.empty.allTitle')).toBeNull();
    expect(listRows()).toHaveLength(0);
    expect(screen.getAllByRole('tab')[0].querySelector('.tab-badge')?.textContent).toBe('0');

    await fireEvent.click(screen.getByTitle('tags.manager.clearSearch'));
    expect(listRows()).toHaveLength(3);
    expect(screen.queryByText(/tags\.manager\.empty\.noMatchTitle/)).toBeNull();
  });
});

describe('the collisions view', () => {
  const clusters = [
    {
      normalized_name: 'standup',
      members: [
        { ...standup, suggested_survivor: true },
        {
          ...standup,
          uuid: K2,
          name: 'Stand-up',
          ownership: 'shared_with_me',
          can_write: false,
          suggested_survivor: false,
        },
      ],
      suggested_survivor_uuid: K1,
      suggestions: [],
    },
    {
      normalized_name: 'retro',
      members: [
        { ...retro, uuid: K3, name: 'Retro', suggested_survivor: true },
        { ...retro, uuid: K4, name: 'retro', suggested_survivor: false },
      ],
      suggested_survivor_uuid: K3,
      suggestions: [],
    },
  ];

  async function collisions() {
    tagsApi.listTagCollisions.mockResolvedValue(clusters);
    await mounted();
    await fireEvent.click(tab('collisions'));
    await waitFor(() => expect(listRows()).toHaveLength(6));
  }

  it('disables the ownership select there and enables it again elsewhere', async () => {
    await mounted();
    expect(scopeSelect()).toBeEnabled();
    tagsApi.listTagCollisions.mockResolvedValue(clusters);
    await fireEvent.click(tab('collisions'));
    await waitFor(() => expect(scopeSelect()).toBeDisabled());
    await fireEvent.click(tab('all'));
    await waitFor(() => expect(scopeSelect()).toBeEnabled());
  });

  it('counts clusters, not members, on the active tab', async () => {
    await collisions();
    expect(screen.getAllByRole('tab')[2].querySelector('.tab-badge')?.textContent).toBe('2');
  });

  it('carries ownership and can_write of a member into the detail pane', async () => {
    await collisions();
    await fireEvent.click(listRows()[2]);
    expect(screen.getByRole('heading', { level: 2, name: 'Stand-up' })).toBeInTheDocument();
    expect(screen.getByText('tags.manager.detail.sharedWithMeHint')).toBeInTheDocument();
    expect(button('tags.manager.detail.rename')).toBeNull();
    expect(button('tags.manager.action.delete')).toBeNull();
    expect(button('tags.manager.share.button')).toBeNull();

    await fireEvent.click(listRows()[1]);
    expect(button('tags.manager.detail.rename')).not.toBeNull();
    expect(button('tags.manager.share.button')).not.toBeNull();
  });

  it('hides merge and delete for a whole cluster that holds a read-only member', async () => {
    await collisions();
    await fireEvent.click(listRows()[0]);
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('"count":2');
    expect(button('tags.manager.action.merge')).toBeNull();
    expect(button('tags.manager.action.delete')).toBeNull();
  });

  it('removes only that cluster when its fully selected header is clicked again', async () => {
    await collisions();
    await fireEvent.click(listRows()[3]);
    await fireEvent.click(listRows()[1], { ctrlKey: true });
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('"count":3');

    await fireEvent.click(listRows()[3]);
    expect(screen.getByRole('heading', { level: 2, name: 'standup' })).toBeInTheDocument();
  });

  it('adds the missing members when a partly selected header is clicked', async () => {
    await collisions();
    await fireEvent.click(listRows()[4]);
    await fireEvent.click(listRows()[3]);
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('"count":2');
    expect((screen.getAllByRole('radio') as HTMLInputElement[]).find((r) => r.checked)?.value).toBe(
      K3
    );
  });
});

describe('promote follows the deployment scope', () => {
  it('is offered to an admin of a single-tenant deployment on a mine tag', async () => {
    caller.role = 'admin';
    await mounted();
    await fireEvent.click(listRows()[0]);
    expect(button('tags.manager.action.promote')).not.toBeNull();
  });

  it.each([['multi' as const], [undefined]])(
    'is withheld from an admin when the tenancy is %s, even on a mine tag',
    async (mode) => {
      caller.role = 'admin';
      setTenancy(mode);
      await mounted();
      await fireEvent.click(listRows()[0]);
      expect(button('tags.manager.detail.rename')).not.toBeNull();
      expect(button('tags.manager.action.promote')).toBeNull();
    }
  );

  it('sends every selected uuid and toasts the promote message', async () => {
    caller.role = 'super_admin';
    setTenancy('multi');
    await mounted();
    await fireEvent.click(listRows()[0]);
    await fireEvent.click(listRows()[2], { shiftKey: true });
    await fireEvent.click(button('tags.manager.action.promote')!);
    await waitFor(() => expect(tagsApi.promoteTags).toHaveBeenCalledWith([K1, K2, K3]));
    await waitFor(() => expect(notify.success).toHaveBeenCalledWith('tags.manager.promote.done'));
  });
});

describe('mutation failures', () => {
  it('falls back to the action-failed key and keeps the selection', async () => {
    tagsApi.deleteTags.mockRejectedValue({ response: { status: 500, data: {} } });
    await mounted();
    await fireEvent.click(listRows()[1]);
    await fireEvent.click(button('tags.manager.action.delete')!);
    await waitFor(() =>
      expect(document.querySelector('[data-testid="confirm-dialog-confirm"]')).not.toBeNull()
    );
    await fireEvent.click(
      document.querySelector('[data-testid="confirm-dialog-confirm"]') as HTMLElement
    );

    await waitFor(() =>
      expect(notify.error).toHaveBeenCalledWith('tags.manager.toast.actionFailed')
    );
    expect(screen.getByRole('heading', { level: 2, name: 'retro' })).toBeInTheDocument();
    expect(tagsApi.listTags).toHaveBeenCalledTimes(1);
  });
});

describe('the share dialog', () => {
  it('closes from its own close button without reloading the list', async () => {
    await mounted();
    await fireEvent.click(listRows()[0]);
    await fireEvent.click(button('tags.manager.share.button')!);
    await waitFor(() => expect(document.querySelector('.tag-share')).not.toBeNull());

    await fireEvent.click(screen.getAllByRole('button', { name: 'modal.closeDialog' }).at(-1)!);
    await waitFor(() => expect(document.querySelector('.tag-share')).toBeNull());
    expect(tagsApi.listTags).toHaveBeenCalledTimes(1);
  });

  it('is not offered from the multi-selection summary', async () => {
    await mounted();
    await fireEvent.click(listRows()[0]);
    await fireEvent.click(listRows()[1], { ctrlKey: true });
    expect(button('tags.manager.share.button')).toBeNull();
  });
});
