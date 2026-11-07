import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import ExportFormatMenu from './ExportFormatMenu.svelte';

const formats = [
  { value: 'csv', label: 'CSV' },
  { value: 'json', label: 'JSON' },
];

function setup(props: Record<string, unknown> = {}) {
  const selected = vi.fn();
  const view = render(ExportFormatMenu, {
    props: { formats, label: 'Export', ...props },
    events: { select: (e: CustomEvent) => selected(e.detail.format) },
  });
  return { ...view, selected };
}

describe('ExportFormatMenu', () => {
  it('lists the formats only after the button is pressed, and dispatches the chosen one', async () => {
    const { selected } = setup();
    expect(screen.queryByRole('menuitem')).toBeNull();

    await fireEvent.click(screen.getByRole('button', { name: /Export/ }));
    expect(screen.getAllByRole('menuitem').map((m) => m.textContent?.trim())).toEqual([
      'CSV',
      'JSON',
    ]);

    await fireEvent.click(screen.getByRole('menuitem', { name: 'JSON' }));
    expect(selected).toHaveBeenCalledExactlyOnceWith('json');
    expect(screen.queryByRole('menuitem')).toBeNull();
  });

  it('closes on Escape without dispatching', async () => {
    const { selected } = setup();
    await fireEvent.click(screen.getByRole('button', { name: /Export/ }));
    await fireEvent.keyDown(screen.getByRole('menuitem', { name: 'CSV' }), { key: 'Escape' });

    expect(screen.queryByRole('menuitem')).toBeNull();
    expect(selected).not.toHaveBeenCalled();
  });

  it('is disabled with the reason as its tooltip, and cannot open', async () => {
    setup({ disabled: true, disabledReason: 'Nothing to export' });
    const button = screen.getByRole('button', { name: /Export/ }) as HTMLButtonElement;

    expect(button.disabled).toBe(true);
    expect(button.title).toBe('Nothing to export');
    await fireEvent.click(button);
    expect(screen.queryByRole('menuitem')).toBeNull();
  });
});
