/**
 * `PromptSettings.svelte` is the summarization-prompt library: system, own and shared
 * prompts, an active selection, a create/edit form with an unsaved-changes guard, an
 * optimistic share toggle and a delete that may have to re-ask the server which prompt is
 * active. These tests drive the real panel and real modals against a faked API.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { SummaryPrompt } from '$lib/api/prompts';

const api = vi.hoisted(() => ({
  getPrompts: vi.fn(),
  getActivePrompt: vi.fn(),
  setActivePrompt: vi.fn(),
  createPrompt: vi.fn(),
  updatePrompt: vi.fn(),
  deletePrompt: vi.fn(),
  sharePrompt: vi.fn(),
  clonePrompt: vi.fn(),
}));
vi.mock('$lib/api/prompts', () => ({ PromptsApi: api }));

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

const clipboard = vi.hoisted(() => ({
  copyToClipboard: vi.fn((_text: string, ok?: () => void) => ok?.()),
}));
vi.mock('$lib/utils/clipboard', () => clipboard);

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import PromptSettings from './PromptSettings.svelte';

const prompt = (o: Partial<SummaryPrompt>): SummaryPrompt =>
  ({
    uuid: 'p',
    name: 'p',
    description: null,
    prompt_text: 'text',
    is_system_default: false,
    user_id: null,
    content_type: 'general',
    is_active: false,
    created_at: '',
    updated_at: '',
    ...o,
  }) as SummaryPrompt;

const SYS_UNIVERSAL = prompt({
  uuid: 'sys-uni',
  name: 'Universal Content Analyzer',
  is_system_default: true,
});
const SYS_HIDDEN = prompt({
  uuid: 'sys-spk',
  name: 'Speaker Identification',
  is_system_default: true,
  content_type: 'speaker_identification',
});
const MINE = prompt({ uuid: 'mine', name: 'My prompt', is_owner: true, is_shared: false });
const THEIRS = prompt({
  uuid: 'theirs',
  name: 'Their prompt',
  is_owner: false,
  author_name: 'Dana',
  author_role: 'admin',
});

function serve(prompts: SummaryPrompt[], activeId: string | null) {
  api.getPrompts.mockResolvedValue({ prompts });
  api.getActivePrompt.mockResolvedValue({
    active_prompt_id: activeId,
    active_prompt: prompts.find((x) => x.uuid === activeId) ?? null,
  });
}

async function mount(props: Record<string, unknown> = {}) {
  const view = render(PromptSettings, { props });
  await waitFor(() => expect(api.getPrompts).toHaveBeenCalled());
  await waitFor(() =>
    expect(view.container.querySelector('.prompt-settings .saved-configs-section')).not.toBeNull()
  );
  return view;
}

const names = (c: HTMLElement) =>
  [...c.querySelectorAll('.config-name')].map((e) => e.textContent?.trim().split(/\s+/)[0]);

beforeEach(() => {
  vi.clearAllMocks();
  api.setActivePrompt.mockResolvedValue(undefined);
  api.deletePrompt.mockResolvedValue(undefined);
  api.sharePrompt.mockResolvedValue(undefined);
});

describe('listing', () => {
  it('shows system, own and shared prompts in their own sections, hiding speaker identification', async () => {
    serve([SYS_UNIVERSAL, SYS_HIDDEN, MINE, THEIRS], 'mine');
    const { container } = await mount();
    const sections = [...container.querySelectorAll('.saved-configs-section')];
    expect(sections).toHaveLength(3);
    expect(names(sections[0] as HTMLElement)).toEqual(['Universal']);
    expect(names(sections[1] as HTMLElement)).toEqual(['My']);
    expect(names(sections[2] as HTMLElement)).toEqual(['Their']);
    expect(container.textContent).not.toContain('Speaker Identification');
    expect(container.querySelector('.admin-badge')).not.toBeNull();
  });

  it('marks the active prompt and offers Activate on the others', async () => {
    serve([SYS_UNIVERSAL, MINE], 'mine');
    const { container } = await mount();
    expect(container.querySelectorAll('.config-item.active')).toHaveLength(1);
    expect(container.querySelectorAll('.currently-active')).toHaveLength(1);
    expect(container.querySelectorAll('.activate-button')).toHaveLength(1);
  });

  it('offers no create button until a custom or shared prompt exists, then an empty state', async () => {
    serve([SYS_UNIVERSAL], 'sys-uni');
    const { container } = await mount();
    expect(container.querySelector('.create-config-button')).toBeNull();
    expect(container.querySelector('.empty-state')).not.toBeNull();
  });
});

describe('active prompt', () => {
  it('activates the universal system prompt when none is selected', async () => {
    const meeting = prompt({ uuid: 'sys-meeting', name: 'Meeting', is_system_default: true });
    serve([meeting, SYS_UNIVERSAL, MINE], null);
    const onSettingsChange = vi.fn();
    await mount({ onSettingsChange });
    await waitFor(() => expect(api.setActivePrompt).toHaveBeenCalledWith({ prompt_id: 'sys-uni' }));
    await waitFor(() => expect(onSettingsChange).toHaveBeenCalled());
  });

  it('activating another prompt reports the change and moves the marker', async () => {
    serve([SYS_UNIVERSAL, MINE], 'sys-uni');
    const onSettingsChange = vi.fn();
    const { container } = await mount({ onSettingsChange });
    await fireEvent.click(container.querySelector('.activate-button') as HTMLElement);
    await waitFor(() => expect(api.setActivePrompt).toHaveBeenCalledWith({ prompt_id: 'mine' }));
    await waitFor(() =>
      expect(container.querySelector('.config-item.active .config-name')?.textContent).toContain(
        'My prompt'
      )
    );
    expect(toast.success).toHaveBeenCalledWith('prompts.activePromptUpdated');
    expect(onSettingsChange).toHaveBeenCalledTimes(1);
  });
});

describe('sharing', () => {
  it('flips the toggle optimistically and keeps it on success', async () => {
    serve([SYS_UNIVERSAL, MINE], 'sys-uni');
    const { container } = await mount();
    const box = container.querySelector('[role="switch"]') as HTMLInputElement;
    await fireEvent.click(box);
    await waitFor(() => expect(api.sharePrompt).toHaveBeenCalledWith('mine', true));
    await waitFor(() => expect(container.querySelector('.share-badge')).not.toBeNull());
    expect(toast.success).toHaveBeenCalledWith('prompts.shareEnabled');
  });

  it('puts the previous state back when the server refuses', async () => {
    serve([SYS_UNIVERSAL, MINE], 'sys-uni');
    api.sharePrompt.mockRejectedValue(new Error('no'));
    const { container } = await mount();
    await fireEvent.click(container.querySelector('[role="switch"]') as HTMLElement);
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(container.querySelector('.share-badge')).toBeNull();
  });
});

describe('delete', () => {
  async function deleteMine(container: HTMLElement) {
    await fireEvent.click(container.querySelector('.delete-config-button') as HTMLElement);
    await fireEvent.click(await screen.findByText('prompts.delete'));
  }

  it('removes the prompt and re-asks the server for the fallback when no user prompt is left', async () => {
    serve([SYS_UNIVERSAL, MINE], 'mine');
    const { container } = await mount();
    api.getActivePrompt.mockResolvedValue({
      active_prompt_id: 'sys-uni',
      active_prompt: SYS_UNIVERSAL,
    });
    await deleteMine(container);
    await waitFor(() => expect(api.deletePrompt).toHaveBeenCalledWith('mine'));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('prompts.promptDeletedWithFallback')
    );
    expect(container.querySelectorAll('.currently-active')).toHaveLength(1);
    expect(container.querySelector('.config-item.active .config-name')?.textContent).toContain(
      'Universal'
    );
  });

  it('only clears the selection when other user prompts remain', async () => {
    const other = prompt({ uuid: 'other', name: 'Other', is_owner: true });
    serve([SYS_UNIVERSAL, MINE, other], 'mine');
    const { container } = await mount();
    api.getActivePrompt.mockClear();
    await deleteMine(container);
    await waitFor(() => expect(api.deletePrompt).toHaveBeenCalledWith('mine'));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('prompts.promptDeleted'));
    expect(api.getActivePrompt).not.toHaveBeenCalled();
    expect(container.querySelectorAll('.config-item.active')).toHaveLength(0);
  });
});

describe('create / edit form', () => {
  async function openCreate(container: HTMLElement) {
    await fireEvent.click(container.querySelector('.create-config-button') as HTMLElement);
    return (await screen.findByLabelText('prompts.promptName')) as HTMLInputElement;
  }

  it('cannot be submitted until it has a name and a body, then creates the prompt', async () => {
    serve([SYS_UNIVERSAL, MINE], 'sys-uni');
    api.createPrompt.mockResolvedValue(prompt({ uuid: 'new', name: 'Fresh', is_owner: true }));
    const onSettingsChange = vi.fn();
    const { container } = await mount({ onSettingsChange });
    const name = await openCreate(container);
    const submit = screen
      .getByText('prompts.createPromptBtn')
      .closest('button') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await fireEvent.input(name, { target: { value: 'Fresh' } });
    expect(submit.disabled).toBe(true);
    await fireEvent.input(screen.getByLabelText('prompts.promptText'), {
      target: { value: 'Body' },
    });
    expect(submit.disabled).toBe(false);
    await fireEvent.click(submit);
    await waitFor(() =>
      expect(api.createPrompt).toHaveBeenCalledWith(
        expect.objectContaining({
          name: 'Fresh',
          prompt_text: 'Body',
          content_type: 'general',
          is_shared: false,
        })
      )
    );
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('prompts.promptCreated'));
    await waitFor(() => expect(container.textContent).toContain('Fresh'));
    expect(onSettingsChange).toHaveBeenCalled();
  });

  it('asks before discarding an edited form, and discards on confirm', async () => {
    serve([SYS_UNIVERSAL, MINE], 'sys-uni');
    const { container } = await mount();
    const name = await openCreate(container);
    await fireEvent.input(name, { target: { value: 'half typed' } });
    await fireEvent.click(screen.getByText('prompts.cancel'));
    expect(await screen.findByText('prompts.unsavedChangesMessage')).toBeTruthy();
    await fireEvent.click(screen.getByText('prompts.discardChanges'));
    await waitFor(() => expect(screen.queryByLabelText('prompts.promptName')).toBeNull());
  });

  it('closes an untouched form without asking', async () => {
    serve([SYS_UNIVERSAL, MINE], 'sys-uni');
    const { container } = await mount();
    await openCreate(container);
    await fireEvent.click(screen.getByText('prompts.cancel'));
    await waitFor(() => expect(screen.queryByLabelText('prompts.promptName')).toBeNull());
    expect(screen.queryByText('prompts.unsavedChangesMessage')).toBeNull();
  });

  it('editing seeds the form from the prompt and updates it in place', async () => {
    serve([SYS_UNIVERSAL, MINE], 'sys-uni');
    api.updatePrompt.mockResolvedValue({ ...MINE, name: 'Renamed' });
    const { container } = await mount();
    await fireEvent.click(container.querySelector('.edit-button') as HTMLElement);
    const name = (await screen.findByLabelText('prompts.promptName')) as HTMLInputElement;
    expect(name.value).toBe('My prompt');
    await fireEvent.input(name, { target: { value: 'Renamed' } });
    await fireEvent.click(screen.getByText('prompts.updatePrompt'));
    await waitFor(() =>
      expect(api.updatePrompt).toHaveBeenCalledWith(
        'mine',
        expect.objectContaining({ name: 'Renamed' })
      )
    );
    await waitFor(() => expect(container.textContent).toContain('Renamed'));
  });
});

describe('view and copy', () => {
  it('shows the prompt text and copies it, flipping the button to its copied state', async () => {
    serve([SYS_UNIVERSAL, MINE], 'sys-uni');
    const { container } = await mount();
    await fireEvent.click(container.querySelector('.view-button') as HTMLElement);
    expect(await screen.findByText('text')).toBeTruthy();
    await fireEvent.click(document.querySelector('.copy-button-header') as HTMLElement);
    expect(clipboard.copyToClipboard).toHaveBeenCalledWith(
      'text',
      expect.any(Function),
      expect.any(Function)
    );
    await waitFor(() =>
      expect(document.querySelector('.copy-button-header.copied')).not.toBeNull()
    );
  });
});
