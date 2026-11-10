import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import Host from './ConfirmationModalTestHost.svelte';

describe('ConfirmationModal Enter key', () => {
  it('does not confirm while it is closed', async () => {
    const onConfirm = vi.fn();
    render(Host, { isOpen: false, onConfirm });

    await fireEvent.keyDown(document.body, { key: 'Enter' });

    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('confirms on Enter while it is open', async () => {
    const onConfirm = vi.fn();
    render(Host, { isOpen: true, onConfirm });

    await fireEvent.keyDown(document.body, { key: 'Enter' });

    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});

describe('ConfirmationModal variants and ARIA', () => {
  it.each([
    ['primary', 'btn-primary'],
    ['danger', 'btn-danger'],
    ['warning', 'btn-warning'],
  ] as const)('%s variant renders the global large %s button with test ids', (variant, cls) => {
    const { getByTestId } = render(Host, { isOpen: true, variant });
    const confirm = getByTestId('confirm-dialog-confirm');
    expect(confirm.classList.contains('btn')).toBe(true);
    expect(confirm.classList.contains('btn-lg')).toBe(true);
    expect(confirm.classList.contains(cls)).toBe(true);
    const cancel = getByTestId('confirm-dialog-cancel');
    expect(cancel.classList.contains('btn-secondary')).toBe(true);
  });

  it('a danger confirm is an alertdialog described by its message', () => {
    const { getByRole } = render(Host, { isOpen: true, variant: 'danger' });
    const dialog = getByRole('alertdialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(
      document.getElementById(dialog.getAttribute('aria-describedby') as string)
    ).not.toBeNull();
  });

  it('a non-destructive confirm stays a plain dialog', () => {
    const { getByRole, queryByRole } = render(Host, { isOpen: true, variant: 'primary' });
    expect(getByRole('dialog')).toBeTruthy();
    expect(queryByRole('alertdialog')).toBeNull();
  });

  it('moves focus to Cancel for a destructive confirm and back to the trigger on close', async () => {
    const { getByTestId, queryByTestId } = render(Host, { isOpen: false, variant: 'danger' });
    const trigger = getByTestId('trigger');
    trigger.focus();
    await fireEvent.click(trigger);
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
    expect(document.activeElement).toBe(getByTestId('confirm-dialog-cancel'));
    await fireEvent.keyDown(document.activeElement as Element, { key: 'Escape' });
    expect(queryByTestId('confirm-dialog-cancel')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('Escape cancels without confirming', async () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(Host, { isOpen: true, variant: 'danger', onConfirm, onCancel });
    await fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('Enter on the focused Cancel button does not also confirm', async () => {
    const onConfirm = vi.fn();
    const { getByTestId } = render(Host, { isOpen: true, variant: 'danger', onConfirm });
    await fireEvent.keyDown(getByTestId('confirm-dialog-cancel'), { key: 'Enter' });
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
