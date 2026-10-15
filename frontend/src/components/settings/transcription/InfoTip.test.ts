import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import Host from './InfoTipTestHost.svelte';

function trigger(name = 'common.moreInformation') {
  return screen.getByRole('button', { name });
}

function panel() {
  return screen.getByRole('tooltip', { hidden: true });
}

const isOpen = () => trigger().getAttribute('aria-expanded') === 'true';

describe('InfoTip', () => {
  it('renders a real focusable button with a default translated accessible name', () => {
    render(Host);
    const btn = trigger();
    expect(btn.tagName).toBe('BUTTON');
    expect(btn).toHaveAttribute('type', 'button');
    expect(btn).toHaveAttribute('aria-expanded', 'false');
  });

  it('uses the label prop as the accessible name when given', () => {
    render(Host, { label: 'How it works' });
    expect(trigger('How it works')).toBeInTheDocument();
  });

  it('links the trigger to the panel via aria-describedby, and the panel holds rich content', async () => {
    render(Host);
    expect(trigger()).not.toHaveAttribute('aria-describedby');
    await fireEvent.click(trigger());
    const p = panel();
    expect(p.id).not.toBe('');
    expect(trigger()).toHaveAttribute('aria-describedby', p.id);
    expect(p.querySelectorAll('li')).toHaveLength(2);
  });

  it('opens on focus', async () => {
    render(Host);
    await fireEvent.focus(trigger());
    expect(isOpen()).toBe(true);
  });

  it('opens on hover, survives a brief gap before the panel, and closes once the pointer is gone', async () => {
    render(Host);
    // The panel is a body child, so it has its own hover handlers sharing the wrapper's grace period.
    const wrapper = trigger().parentElement as HTMLElement;
    await fireEvent.mouseEnter(wrapper);
    expect(isOpen()).toBe(true);
    await fireEvent.mouseLeave(wrapper);
    expect(isOpen()).toBe(true);
    await fireEvent.mouseEnter(panel());
    await new Promise((r) => setTimeout(r, 300));
    expect(isOpen()).toBe(true);
    await fireEvent.mouseLeave(panel());
    await new Promise((r) => setTimeout(r, 300));
    expect(isOpen()).toBe(false);
  });

  it('click toggles it open and closed', async () => {
    render(Host);
    await fireEvent.click(trigger());
    expect(isOpen()).toBe(true);
    await fireEvent.click(trigger());
    expect(isOpen()).toBe(false);
  });

  it('Escape closes it without reaching other Escape handlers', async () => {
    const outer = vi.fn();
    document.addEventListener('keydown', outer);
    render(Host);
    await fireEvent.click(trigger());
    await fireEvent.keyDown(trigger(), { key: 'Escape' });
    expect(isOpen()).toBe(false);
    expect(outer).not.toHaveBeenCalled();
    // With nothing open, Escape is left alone for the surrounding dialog.
    await fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(outer).toHaveBeenCalledTimes(1);
    document.removeEventListener('keydown', outer);
  });

  it('closes on an outside click', async () => {
    render(Host);
    await fireEvent.click(trigger());
    expect(isOpen()).toBe(true);
    await fireEvent.pointerDown(screen.getByTestId('outside'));
    expect(isOpen()).toBe(false);
  });

  it('does not close on a pointer press inside the panel', async () => {
    render(Host);
    await fireEvent.click(trigger());
    await fireEvent.pointerDown(panel().querySelector('li') as HTMLElement);
    expect(isOpen()).toBe(true);
  });

  it('renders the panel on <body> through anchoredPanel, not inside the trigger wrapper', async () => {
    render(Host);
    expect(screen.queryByRole('tooltip', { hidden: true })).toBeNull();
    await fireEvent.click(trigger());
    const p = panel();
    expect(p.parentElement).toBe(document.body);
    expect(p.style.position).toBe('fixed');
  });

  it('closes when focus leaves to an element outside it', async () => {
    render(Host);
    await fireEvent.focus(trigger());
    await fireEvent.focusOut(trigger(), { relatedTarget: screen.getByTestId('outside') });
    expect(isOpen()).toBe(false);
  });
});
