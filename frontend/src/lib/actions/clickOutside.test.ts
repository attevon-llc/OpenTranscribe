import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { clickOutside } from './clickOutside';

describe('clickOutside action', () => {
  let node: HTMLElement;
  let outside: HTMLElement;
  let handler: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    node = document.createElement('div');
    outside = document.createElement('button');
    document.body.append(node, outside);
    handler = vi.fn();
    // vitest 4's Mock type no longer structurally matches EventListener
    node.addEventListener('click_outside', handler as unknown as EventListener);
  });

  afterEach(() => {
    node.remove();
    outside.remove();
  });

  const click = (el: HTMLElement) => el.dispatchEvent(new MouseEvent('click', { bubbles: true }));

  it('fires on an outside click', () => {
    const action = clickOutside(node);
    click(outside);
    expect(handler).toHaveBeenCalledTimes(1);
    action.destroy();
  });

  it('does not fire on an inside click', () => {
    const inner = document.createElement('span');
    node.append(inner);
    const action = clickOutside(node);
    click(inner);
    expect(handler).not.toHaveBeenCalled();
    action.destroy();
  });

  it('does not fire when disabled', () => {
    const action = clickOutside(node, { enabled: false });
    click(outside);
    expect(handler).not.toHaveBeenCalled();
    action.destroy();
  });

  it('does not fire when the click is inside an ignored element', () => {
    const action = clickOutside(node, { ignore: [outside] });
    click(outside);
    expect(handler).not.toHaveBeenCalled();
    action.destroy();
  });

  it('honors enabled toggled via update()', () => {
    const action = clickOutside(node, { enabled: false });
    action.update({ enabled: true });
    click(outside);
    expect(handler).toHaveBeenCalledTimes(1);
    action.destroy();
  });

  it('stops firing after destroy()', () => {
    const action = clickOutside(node);
    action.destroy();
    click(outside);
    expect(handler).not.toHaveBeenCalled();
  });
});
