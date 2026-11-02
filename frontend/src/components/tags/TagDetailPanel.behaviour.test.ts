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
import type { TagImpact } from '$lib/types/tag';

const FIRST = '70000000-0000-4000-8000-000000000007';
const SECOND = '80000000-0000-4000-8000-000000000008';

const roadmap: TagListEntry = {
  uuid: FIRST,
  name: 'Roadmap',
  source: 'manual',
  usage_count: 4,
  ownership: 'mine',
  can_write: true,
};
const hiring: TagListEntry = { ...roadmap, uuid: SECOND, name: 'Hiring' };

const preview: TagImpact = { tags: [], accessible_file_count: 2, total_file_count: 9 };

const field = () =>
  screen.getByLabelText('tags.manager.detail.renameLabel') as HTMLInputElement | null;
const maybeField = () => screen.queryByLabelText('tags.manager.detail.renameLabel');
const renameButton = () => screen.getByRole('button', { name: 'tags.manager.detail.rename' });
const confirmButton = () => document.querySelector('.modal-delete-button') as HTMLElement | null;
const cancelButton = () => document.querySelector('.modal-cancel-button') as HTMLElement | null;

async function openEditor(props: Record<string, unknown> = {}, events = {}) {
  const view = render(TagDetailPanel, { props: { tag: roadmap, ...props }, events });
  await fireEvent.click(renameButton());
  return view;
}

describe('a name that normalizes to nothing', () => {
  it('is refused locally with an alert that the field points at', async () => {
    const rename = vi.fn();
    await openEditor({}, { rename });
    await fireEvent.input(field()!, { target: { value: ' -_- ' } });
    await fireEvent.keyDown(field()!, { key: 'Enter' });

    expect(rename).not.toHaveBeenCalled();
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('tags.nameRequired');
    expect(field()).toHaveAttribute('aria-invalid', 'true');
    expect(field()!.getAttribute('aria-describedby')).toBe(alert.id);
    expect(alert.id).not.toBe('');
  });

  it('drops the error as soon as the user types again', async () => {
    await openEditor();
    await fireEvent.input(field()!, { target: { value: '---' } });
    await fireEvent.click(renameButton());
    expect(screen.getByRole('alert')).toBeInTheDocument();

    await fireEvent.input(field()!, { target: { value: '---x' } });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(field()).not.toHaveAttribute('aria-invalid');
    expect(field()).not.toHaveAttribute('aria-describedby');
  });

  it('is dropped by Cancel, and reopening shows the current name with no error', async () => {
    await openEditor();
    await fireEvent.input(field()!, { target: { value: '__' } });
    await fireEvent.click(renameButton());
    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.action.cancel' }));
    expect(maybeField()).toBeNull();

    await fireEvent.click(renameButton());
    expect(field()!.value).toBe('Roadmap');
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('the field clamps to 50 code points', () => {
  it('cuts pasted emoji to 50 and sends that many', async () => {
    const rename = vi.fn();
    await openEditor({}, { rename });
    await fireEvent.input(field()!, { target: { value: '\u{1F680}'.repeat(70) } });
    expect(field()!.value).toBe('\u{1F680}'.repeat(50));
    await fireEvent.keyDown(field()!, { key: 'Enter' });
    expect(rename.mock.calls.map((call) => call[0].detail)).toEqual([
      { name: '\u{1F680}'.repeat(50) },
    ]);
  });

  it('carries no native maxlength', async () => {
    await openEditor();
    expect(field()).not.toHaveAttribute('maxlength');
  });
});

describe('moving to another tag', () => {
  it('closes the editor and forgets the text typed for the previous tag', async () => {
    const rename = vi.fn();
    const view = await openEditor({}, { rename });
    await fireEvent.input(field()!, { target: { value: 'Typed for roadmap' } });

    await view.rerender({ tag: hiring });
    expect(maybeField()).toBeNull();
    expect(screen.getByRole('heading', { level: 2, name: 'Hiring' })).toBeInTheDocument();

    await fireEvent.click(renameButton());
    expect(field()!.value).toBe('Hiring');
    await fireEvent.keyDown(field()!, { key: 'Enter' });
    expect(rename).not.toHaveBeenCalled();
  });

  it('drops the remembered submitted name, so a later confirm sends nothing', async () => {
    const confirmRenameMerge = vi.fn();
    const view = await openEditor({}, { confirmRenameMerge });
    await fireEvent.input(field()!, { target: { value: 'Budget' } });
    await fireEvent.click(renameButton());

    await view.rerender({ tag: hiring });
    await view.rerender({ tag: hiring, renameMergeImpact: preview });
    await fireEvent.click(
      screen.getByRole('button', { name: 'tags.manager.detail.confirmMergeRename' })
    );
    expect(confirmRenameMerge).not.toHaveBeenCalled();
  });

  it('keeps the editor open when only the same tag is refreshed', async () => {
    const view = await openEditor();
    await fireEvent.input(field()!, { target: { value: 'Draft' } });
    await view.rerender({ tag: { ...roadmap, usage_count: 99 } });
    expect(field()!.value).toBe('Draft');
  });

  it('follows a rename of the shown tag in the heading while not editing', async () => {
    const view = render(TagDetailPanel, { props: { tag: roadmap } });
    await view.rerender({ tag: { ...roadmap, name: 'Roadmap 2026' } });
    expect(screen.getByRole('heading', { level: 2, name: 'Roadmap 2026' })).toBeInTheDocument();
    await fireEvent.click(renameButton());
    expect(field()!.value).toBe('Roadmap 2026');
  });
});

describe('merge warning and the editor', () => {
  it('keep editing leaves the field open with the typed text', async () => {
    const view = await openEditor();
    await fireEvent.input(field()!, { target: { value: 'Budget' } });
    await fireEvent.click(renameButton());
    await view.rerender({ tag: roadmap, renameMergeImpact: preview });

    await fireEvent.click(screen.getByRole('button', { name: 'tags.manager.detail.keepEditing' }));
    expect(field()!.value).toBe('Budget');
  });

  it('typing with no warning up emits no cancelRenameMerge', async () => {
    const cancelRenameMerge = vi.fn();
    await openEditor({}, { cancelRenameMerge });
    await fireEvent.input(field()!, { target: { value: 'Other' } });
    expect(cancelRenameMerge).not.toHaveBeenCalled();
  });
});

describe('the header rename button', () => {
  it.each(['rename', 'delete', 'promote'] as const)('is disabled while %s runs', (busy) => {
    render(TagDetailPanel, { props: { tag: roadmap, busy } });
    expect(renameButton()).toBeDisabled();
  });
});

describe('the shared delete dialog', () => {
  it('opens again for a new preview after a confirm closed it', async () => {
    const confirmDelete = vi.fn();
    const view = render(TagDetailPanel, {
      props: { tag: roadmap, deletePreview: preview },
      events: { confirmDelete },
    });
    await fireEvent.click(confirmButton()!);
    expect(confirmDelete).toHaveBeenCalledTimes(1);
    expect(confirmButton()).toBeNull();

    await view.rerender({ tag: roadmap, deletePreview: { ...preview, accessible_file_count: 1 } });
    expect(confirmButton()).not.toBeNull();
    expect(
      screen.getByText(
        'tags.manager.impact.deleteMessage {"name":"Roadmap","accessible":1,"total":9}'
      )
    ).toBeInTheDocument();
  });

  it('opens again when a fresh preview replaces the old one without a null in between', async () => {
    const view = render(TagDetailPanel, { props: { tag: roadmap, deletePreview: preview } });
    await fireEvent.click(cancelButton()!);
    expect(confirmButton()).toBeNull();
    await view.rerender({ tag: roadmap, deletePreview: { ...preview } });
    expect(confirmButton()).not.toBeNull();
  });

  it('refuses a confirm while busy and accepts it once the panel is idle again', async () => {
    const confirmDelete = vi.fn();
    const view = render(TagDetailPanel, {
      props: { tag: roadmap, deletePreview: preview, busy: 'promote' },
      events: { confirmDelete },
    });
    await fireEvent.click(confirmButton()!);
    expect(confirmDelete).not.toHaveBeenCalled();

    await view.rerender({ tag: roadmap, deletePreview: { ...preview }, busy: null });
    await fireEvent.click(confirmButton()!);
    expect(confirmDelete).toHaveBeenCalledTimes(1);
  });

  it('closes when the preview is withdrawn', async () => {
    const view = render(TagDetailPanel, { props: { tag: roadmap, deletePreview: preview } });
    await view.rerender({ tag: roadmap, deletePreview: null });
    expect(confirmButton()).toBeNull();
  });
});
