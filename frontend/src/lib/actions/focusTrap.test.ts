import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { focusTrap } from './focusTrap';

/** Wait for the requestAnimationFrame callback the action uses to defer focus. */
const nextFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

const tab = (node: HTMLElement, shiftKey = false) => {
  const event = new KeyboardEvent('keydown', {
    key: 'Tab',
    shiftKey,
    bubbles: true,
    cancelable: true,
  });
  node.dispatchEvent(event);
  return event;
};

describe('focusTrap action', () => {
  let container: HTMLElement;
  let first: HTMLButtonElement;
  let last: HTMLButtonElement;
  let outside: HTMLButtonElement;

  beforeEach(() => {
    container = document.createElement('div');
    first = document.createElement('button');
    first.textContent = 'first';
    last = document.createElement('button');
    last.textContent = 'last';
    container.append(first, last);

    outside = document.createElement('button');
    outside.textContent = 'outside';

    document.body.append(outside, container);
  });

  afterEach(() => {
    container.remove();
    outside.remove();
  });

  it('focuses the first focusable element when enabled', async () => {
    outside.focus();
    const action = focusTrap(container, { enabled: true });
    await nextFrame();
    expect(document.activeElement).toBe(first);
    action.destroy();
  });

  it('cycles from last to first on Tab and first to last on Shift+Tab', async () => {
    const action = focusTrap(container, { enabled: true });
    await nextFrame();

    last.focus();
    const fwd = tab(container, false);
    expect(fwd.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(first);

    first.focus();
    const back = tab(container, true);
    expect(back.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(last);

    action.destroy();
  });

  it('restores focus to the previously-focused element on destroy', async () => {
    outside.focus();
    expect(document.activeElement).toBe(outside);

    const action = focusTrap(container, { enabled: true });
    await nextFrame();
    expect(document.activeElement).toBe(first);

    action.destroy();
    expect(document.activeElement).toBe(outside);
  });

  it('does nothing while disabled', async () => {
    outside.focus();
    const action = focusTrap(container, { enabled: false });
    await nextFrame();
    expect(document.activeElement).toBe(outside);

    const event = tab(container, false);
    expect(event.defaultPrevented).toBe(false);
    action.destroy();
  });
});
