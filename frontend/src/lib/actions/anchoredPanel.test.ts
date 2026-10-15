import { describe, it, expect, afterEach, vi } from 'vitest';
import { anchoredPanel } from './anchoredPanel';

const VIEW = { width: 1280, height: 600 };

function rect(top: number, left: number, width: number, height: number): DOMRect {
  return {
    top,
    left,
    width,
    height,
    right: left + width,
    bottom: top + height,
    x: left,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}

/** jsdom has no layout: give the anchor a box and the panel a natural size. */
function setup(anchorBox: DOMRect, panel = { width: 240, height: 300 }) {
  const anchor = document.createElement('button');
  anchor.getBoundingClientRect = () => anchorBox;
  const node = document.createElement('div');
  node.getBoundingClientRect = () => rect(0, 0, panel.width, panel.height);
  document.body.append(anchor, node);
  return { anchor, node, panel };
}

function viewport(width = VIEW.width, height = VIEW.height) {
  const h = vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(height);
  vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(width);
  return h;
}

describe('anchoredPanel', () => {
  const cleanups: Array<() => void> = [];
  afterEach(() => {
    cleanups.splice(0).forEach((c) => c());
    document.body.innerHTML = '';
    document.documentElement.removeAttribute('dir');
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
  function mount(node: HTMLElement, opts: Parameters<typeof anchoredPanel>[1]) {
    const handle = anchoredPanel(node, opts);
    cleanups.push(() => handle.destroy());
    return handle;
  }

  it('opens below when the panel fits, as a fixed layer on <body>', () => {
    viewport();
    const { anchor, node } = setup(rect(100, 50, 240, 38));
    mount(node, { anchor });
    expect(node.dataset.side).toBe('below');
    expect(node.style.position).toBe('fixed');
    expect(node.style.top).toBe(`${100 + 38 + 6}px`);
    expect(node.style.left).toBe('50px');
    expect(node.parentElement).toBe(document.body);
  });

  it('flips above and keeps the panel inside the viewport near the bottom', () => {
    viewport();
    const { anchor, node } = setup(rect(520, 50, 240, 38), { width: 240, height: 300 });
    mount(node, { anchor });
    expect(node.dataset.side).toBe('above');
    const top = parseFloat(node.style.top);
    expect(top).toBeGreaterThanOrEqual(8);
    expect(top + 300).toBeLessThanOrEqual(520 - 6);
  });

  it('caps maxHeight to the room available so the panel scrolls internally', () => {
    viewport();
    const { anchor, node } = setup(rect(300, 50, 240, 38), { width: 240, height: 900 });
    mount(node, { anchor });
    const cap = parseFloat(node.style.maxHeight);
    expect(cap).toBeGreaterThan(0);
    expect(cap).toBeLessThan(600);
    expect(parseFloat(node.style.top) + cap).toBeLessThanOrEqual(600 - 8 + 1);
  });

  it('recomputes on window resize', () => {
    const h = viewport(VIEW.width, 1000);
    const { anchor, node } = setup(rect(520, 50, 240, 38));
    mount(node, { anchor });
    expect(node.dataset.side).toBe('below');
    h.mockReturnValue(600);
    window.dispatchEvent(new Event('resize'));
    expect(node.dataset.side).toBe('above');
  });

  it('recomputes when any ancestor scrolls (capture) and the anchor moved', () => {
    viewport();
    let box = rect(100, 50, 240, 38);
    const { anchor, node } = setup(box);
    anchor.getBoundingClientRect = () => box;
    mount(node, { anchor });
    expect(node.dataset.side).toBe('below');
    box = rect(540, 50, 240, 38);
    const scroller = document.createElement('div');
    document.body.append(scroller);
    scroller.dispatchEvent(new Event('scroll'));
    expect(node.dataset.side).toBe('above');
  });

  it('re-places when the panel content height changes (ResizeObserver)', () => {
    viewport();
    let callback: () => void = () => {};
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          callback = cb;
        }
        observe() {}
        disconnect() {}
      }
    );
    const { anchor, node, panel } = setup(rect(400, 50, 240, 38), { width: 240, height: 100 });
    mount(node, { anchor });
    expect(node.dataset.side).toBe('below');
    panel.height = 400;
    callback();
    expect(node.dataset.side).toBe('above');
  });

  it('aligns to the right edge of the anchor in RTL and stays inside the viewport', () => {
    viewport();
    const { anchor, node } = setup(rect(100, 800, 200, 38), { width: 320, height: 100 });
    mount(node, { anchor, rtl: true, width: 'content' });
    expect(parseFloat(node.style.left)).toBe(800 + 200 - 320);
    const second = setup(rect(100, 1200, 200, 38), { width: 320, height: 100 });
    mount(second.node, { anchor: second.anchor, rtl: true, width: 'content' });
    expect(parseFloat(second.node.style.left) + 320).toBeLessThanOrEqual(1280 - 8);
  });

  it('reads rtl from the document when not given', () => {
    viewport();
    document.documentElement.dir = 'rtl';
    const { anchor, node } = setup(rect(100, 800, 200, 38), { width: 320, height: 100 });
    mount(node, { anchor, width: 'content' });
    expect(parseFloat(node.style.left)).toBe(800 + 200 - 320);
  });

  it('matches the anchor width by default', () => {
    viewport();
    const { anchor, node } = setup(rect(100, 50, 260, 38));
    mount(node, { anchor });
    expect(node.style.width).toBe('260px');
  });

  it('removes its listeners and the node on destroy', () => {
    viewport();
    const removeWin = vi.spyOn(window, 'removeEventListener');
    const removeDoc = vi.spyOn(document, 'removeEventListener');
    const { anchor, node } = setup(rect(100, 50, 240, 38));
    const handle = anchoredPanel(node, { anchor });
    handle.destroy();
    expect(removeWin).toHaveBeenCalledWith('resize', expect.any(Function));
    expect(removeDoc).toHaveBeenCalledWith('scroll', expect.any(Function), true);
    expect(node.isConnected).toBe(false);
  });
});
