import { describe, it, expect } from 'vitest';
import { computePopoverPlacement } from './popoverPosition';

const VIEW = { width: 1280, height: 800 };
const trigger = (
  over: Partial<{ top: number; left: number; width: number; height: number }> = {}
) => ({
  top: 200,
  left: 100,
  width: 240,
  height: 38,
  ...over,
});

describe('computePopoverPlacement', () => {
  it('opens below the trigger, aligned to its start edge, at least minWidth wide', () => {
    const p = computePopoverPlacement(trigger(), 380, VIEW);
    expect(p.side).toBe('below');
    expect(p.top).toBe(200 + 38 + 6);
    expect(p.left).toBe(100);
    expect(p.width).toBe(300);
    expect(p.sheet).toBe(false);
  });

  it('flips above when it does not fit below and there is more room above', () => {
    const p = computePopoverPlacement(trigger({ top: 700 }), 380, VIEW);
    expect(p.side).toBe('above');
    expect(p.top + 380).toBeLessThanOrEqual(700 - 6);
  });

  it('stays below, capped by maxHeight, when neither side fits but below has more room', () => {
    const p = computePopoverPlacement(trigger({ top: 100 }), 900, VIEW);
    expect(p.side).toBe('below');
    expect(p.maxHeight).toBeLessThan(900);
  });

  it('clamps to the right viewport edge', () => {
    const p = computePopoverPlacement(trigger({ left: 1200, width: 200 }), 300, VIEW);
    expect(p.left + p.width).toBeLessThanOrEqual(VIEW.width - 8);
  });

  it('clamps to the left viewport edge', () => {
    const p = computePopoverPlacement(trigger({ left: -40 }), 300, VIEW);
    expect(p.left).toBe(8);
  });

  it('aligns to the trigger end edge in RTL', () => {
    const p = computePopoverPlacement(trigger({ left: 600, width: 240 }), 300, VIEW, { rtl: true });
    expect(p.left + p.width).toBe(600 + 240);
  });

  it('becomes a full-width bottom sheet on a narrow viewport', () => {
    const p = computePopoverPlacement(trigger(), 300, { width: 390, height: 800 });
    expect(p.sheet).toBe(true);
    expect(p.width).toBe(390);
    expect(p.maxHeight).toBeLessThanOrEqual(800 * 0.85);
  });
});
