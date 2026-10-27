/**
 * The purge confirmation (issue #1280). A purge is irreversible, so the destructive button must
 * stay disabled until the person has ticked "I understand" AND typed the literal word the API
 * requires; a stale confirmation must never survive closing and reopening; and a refusal from
 * the server is shown, not swallowed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      en[key] ?? key
    );
  return { t: readable(t), locale: readable('en') };
});

const status = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/privacy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/privacy')>();
  return { ...actual, PrivacyApi: { getPurgeStatus: status } };
});

import PurgeConfirmDialog from './PurgeConfirmDialog.svelte';
import type { PurgeAccepted, PurgeTarget } from '$lib/api/privacy';

interface DialogProps {
  isOpen: boolean;
  onClose: () => void;
  targets: PurgeTarget[];
  scopeLabel: string;
  start: () => Promise<PurgeAccepted>;
  pollMs: number;
}

const props = (over: Partial<DialogProps> = {}) => ({
  isOpen: true,
  onClose: vi.fn(),
  targets: ['voiceprints'] as PurgeTarget[],
  scopeLabel: 'everyone in your organization',
  start: vi.fn().mockResolvedValue({ task_id: 't-1' }) as unknown as DialogProps['start'] &
    ReturnType<typeof vi.fn>,
  pollMs: 5,
  ...over,
});

const deleteButton = () => screen.getByTestId('purge-confirm') as HTMLButtonElement;
const understood = () => screen.getByRole('checkbox') as HTMLInputElement;
const typedBox = () => screen.getByRole('textbox') as HTMLInputElement;

beforeEach(() => {
  vi.clearAllMocks();
});

describe('PurgeConfirmDialog', () => {
  it('says what is deleted, what is kept, who is affected and that it is irreversible', () => {
    render(PurgeConfirmDialog, props({ targets: ['voiceprints', 'inferred_gender'] }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Affected: everyone in your organization');
    expect(dialog).toHaveTextContent('Stored voiceprints');
    expect(dialog).toHaveTextContent('Gender that was inferred from voice');
    expect(dialog).toHaveTextContent('Kept: speaker profiles');
    expect(dialog).toHaveTextContent('This cannot be undone');
  });

  it('lists only the targets it was given', () => {
    render(PurgeConfirmDialog, props({ targets: ['inferred_gender'] }));
    expect(screen.getByRole('dialog')).not.toHaveTextContent('Stored voiceprints');
  });

  it('keeps the delete button disabled until BOTH the box is ticked and the word is typed', async () => {
    render(PurgeConfirmDialog, props());
    expect(deleteButton()).toBeDisabled();

    await fireEvent.click(understood());
    expect(deleteButton()).toBeDisabled();

    await fireEvent.input(typedBox(), { target: { value: 'PURG' } });
    expect(deleteButton()).toBeDisabled();

    await fireEvent.input(typedBox(), { target: { value: 'PURGE' } });
    expect(deleteButton()).toBeEnabled();

    await fireEvent.click(understood());
    expect(deleteButton()).toBeDisabled();
  });

  it('requires the exact word: lower case and padding do not count', async () => {
    render(PurgeConfirmDialog, props());
    await fireEvent.click(understood());
    for (const wrong of ['purge', 'Purge', ' PURGE', 'PURGE ']) {
      await fireEvent.input(typedBox(), { target: { value: wrong } });
      expect(deleteButton()).toBeDisabled();
    }
  });

  it('does not call the server until confirmed, then calls it exactly once', async () => {
    const p = props();
    render(PurgeConfirmDialog, p);
    await fireEvent.click(deleteButton());
    expect(p.start).not.toHaveBeenCalled();

    status.mockResolvedValue({ task_id: 't-1', status: 'completed', counts: { speakers: 4 } });
    await fireEvent.click(understood());
    await fireEvent.input(typedBox(), { target: { value: 'PURGE' } });
    await fireEvent.click(deleteButton());
    expect(p.start).toHaveBeenCalledTimes(1);
  });

  it('follows the purge to completion and reports counts only', async () => {
    status
      .mockResolvedValueOnce({ task_id: 't-1', status: 'in_progress', counts: null })
      .mockResolvedValue({ task_id: 't-1', status: 'completed', counts: { speakers: 3, docs: 2 } });
    render(PurgeConfirmDialog, props());
    await fireEvent.click(understood());
    await fireEvent.input(typedBox(), { target: { value: 'PURGE' } });
    await fireEvent.click(deleteButton());

    await waitFor(() =>
      expect(screen.getByRole('dialog')).toHaveTextContent('Deletion finished. Items removed: 5')
    );
    expect(status).toHaveBeenCalledWith('t-1');
    expect(screen.queryByTestId('purge-confirm')).toBeNull();
  });

  it('shows the server refusal and leaves the button usable for a retry', async () => {
    const start = vi.fn().mockRejectedValue({
      response: { status: 409, data: { detail: { code: 'policy_still_enabled' } } },
    });
    render(PurgeConfirmDialog, props({ start }));
    await fireEvent.click(understood());
    await fireEvent.input(typedBox(), { target: { value: 'PURGE' } });
    await fireEvent.click(deleteButton());

    expect(await screen.findByRole('alert')).toHaveTextContent('Turn this feature off first');
    expect(status).not.toHaveBeenCalled();
    expect(deleteButton()).toBeEnabled();
  });

  it('falls back to a generic sentence for an error it has no copy for', async () => {
    render(PurgeConfirmDialog, props({ start: vi.fn().mockRejectedValue(new Error('network')) }));
    await fireEvent.click(understood());
    await fireEvent.input(typedBox(), { target: { value: 'PURGE' } });
    await fireEvent.click(deleteButton());
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The deletion could not be started. Nothing was deleted.'
    );
  });

  it('reports a failed purge as a failure', async () => {
    status.mockResolvedValue({ task_id: 't-1', status: 'failed', counts: null });
    render(PurgeConfirmDialog, props());
    await fireEvent.click(understood());
    await fireEvent.input(typedBox(), { target: { value: 'PURGE' } });
    await fireEvent.click(deleteButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('The deletion failed');
  });

  it('starts from a clean slate every time it opens', async () => {
    const p = props();
    const view = render(PurgeConfirmDialog, p);
    await fireEvent.click(understood());
    await fireEvent.input(typedBox(), { target: { value: 'PURGE' } });
    expect(deleteButton()).toBeEnabled();

    await view.rerender({ ...p, isOpen: false });
    await view.rerender({ ...p, isOpen: true });

    expect(understood().checked).toBe(false);
    expect(typedBox().value).toBe('');
    expect(deleteButton()).toBeDisabled();
  });

  it('cancel closes without calling the server', async () => {
    const p = props();
    render(PurgeConfirmDialog, p);
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(p.onClose).toHaveBeenCalled();
    expect(p.start).not.toHaveBeenCalled();
  });

  it('is a labelled modal dialog and moves focus inside it', async () => {
    render(PurgeConfirmDialog, props());
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Delete stored data permanently');
    // The focus trap defers to the next frame.
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
  });
});
