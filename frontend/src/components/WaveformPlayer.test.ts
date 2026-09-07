/**
 * `WaveformPlayer.svelte` picks a fetch resolution from device/bandwidth signals,
 * draws bars onto a raw `<canvas>` (no library — hand-rolled x/y math), and turns
 * click/drag/keyboard input into seek times via pure geometry. None of that throws
 * if it's wrong; it just silently seeks to (or renders) the wrong position.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import WaveformPlayer from './WaveformPlayer.svelte';

function fakeCtx() {
  return {
    clearRect: vi.fn(),
    fillRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
  };
}

let ctx: ReturnType<typeof fakeCtx>;
const originalGetContext = HTMLCanvasElement.prototype.getContext;

function rootOf(container: HTMLElement): HTMLElement {
  return container.querySelector('.waveform-container') as HTMLElement;
}

function canvasOf(container: HTMLElement): HTMLCanvasElement {
  return container.querySelector('canvas.waveform-canvas') as HTMLCanvasElement;
}

// `loadWaveformData()` (and its `getOptimalResolution()` container-width read)
// now runs SYNCHRONOUSLY during `onMount` (#649 removed the artificial 100ms
// sleep that used to separate them), i.e. before `render()` even returns — so
// a test can no longer stub `offsetWidth` on the rendered instance afterward
// and expect it to matter. Patch the prototype getter once, and let each test
// set the value it wants BEFORE calling `render()`.
let mockedContainerWidth = 0;
Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
  configurable: true,
  get() {
    return mockedContainerWidth;
  },
});

function setContainerWidth(width: number) {
  mockedContainerWidth = width;
}

// The microtask chain of the mocked axios call — real, so nothing races
// fake-timer flushing. `onMount` no longer sleeps before firing it (#649);
// this only waits for the axios promise itself to settle.
async function waitForMountedLoad() {
  await new Promise((resolve) => setTimeout(resolve, 20));
}

/** Let one requestAnimationFrame callback run (drag seeks are rAF-coalesced). */
async function nextAnimationFrame() {
  await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  vi.clearAllMocks();
  ctx = fakeCtx();
  HTMLCanvasElement.prototype.getContext = vi.fn(
    () => ctx
  ) as unknown as typeof HTMLCanvasElement.prototype.getContext;
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 });
  Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: 1 });
  Object.defineProperty(window.navigator, 'connection', { configurable: true, value: undefined });
  mockedContainerWidth = 300;
});

afterEach(() => {
  HTMLCanvasElement.prototype.getContext =
    originalGetContext as typeof HTMLCanvasElement.prototype.getContext;
  vi.unstubAllGlobals();
});

describe('resolution selection', () => {
  it('requests the small resolution for a narrow (mobile) viewport', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 500 });
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    setContainerWidth(300);
    const { container } = render(WaveformPlayer, { props: { fileId: 'f1' } });

    await waitForMountedLoad();

    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1/waveform', { params: { samples: 500 } });
    // The chosen resolution must actually be usable, not just requested.
    expect(canvasOf(container).classList.contains('hidden')).toBe(false);
  });

  it('requests the large resolution for a high-DPI desktop viewport', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1600 });
    Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: 2 });
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    setContainerWidth(1400);
    const { container } = render(WaveformPlayer, { props: { fileId: 'f1' } });

    await waitForMountedLoad();

    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1/waveform', { params: { samples: 2000 } });
    expect(canvasOf(container).classList.contains('hidden')).toBe(false);
  });

  it('always requests the small resolution on a slow connection, regardless of screen size', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1600 });
    Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: 2 });
    Object.defineProperty(window.navigator, 'connection', {
      configurable: true,
      value: { effectiveType: '2g', downlink: 0.2 },
    });
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    setContainerWidth(1400);
    const { container } = render(WaveformPlayer, { props: { fileId: 'f1' } });

    await waitForMountedLoad();

    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1/waveform', { params: { samples: 500 } });
    expect(canvasOf(container).classList.contains('hidden')).toBe(false);
  });
});

describe('loading waveform data', () => {
  it('fetches the waveform on mount without waiting for a timer (#649)', async () => {
    // DEFECT THIS CATCHES: `onMount` used to sleep 100ms via `setTimeout`
    // before calling `loadWaveformData()`, even though `bind:this` assigns
    // `canvas`/`container` before `onMount` fires and the fetch itself
    // doesn't touch the DOM. That silently dead-weighted every waveform
    // fetch by 100ms on a fresh page load. Only microtask turns are awaited
    // below — no timer tick at all — so this fails under the old code.
    mockAxios.get.mockResolvedValue({ data: { waveform: [10, 200, 50] } });
    const { container } = render(WaveformPlayer, { props: { fileId: 'f1', duration: 10 } });

    await Promise.resolve();
    await Promise.resolve();

    // Default test width (300px, set in beforeEach) falls in the "small"
    // resolution bucket at desktop innerWidth — see 'resolution selection'
    // above for the bucket boundaries.
    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1/waveform', { params: { samples: 500 } });

    // A called mock proves the REQUEST went out immediately; it does not
    // prove the fetched data ever reached the component. Wait for the
    // real axios promise to settle and assert the waveform actually
    // rendered — same outcome as 'draws bars once data arrives' below, just
    // reached without an artificial timer in between.
    await waitForMountedLoad();
    expect(container.querySelector('.waveform-loading')).toBeNull();
    expect(ctx.fillRect).toHaveBeenCalled();
    expect(canvasOf(container).classList.contains('hidden')).toBe(false);
  });

  it('draws bars once data arrives and hides the loading overlay', async () => {
    mockAxios.get.mockResolvedValue({ data: { waveform: [10, 200, 50] } });
    const { container } = render(WaveformPlayer, { props: { fileId: 'f1', duration: 10 } });

    await waitForMountedLoad();

    expect(container.querySelector('.waveform-loading')).toBeNull();
    expect(ctx.fillRect).toHaveBeenCalled();
    expect(canvasOf(container).classList.contains('hidden')).toBe(false);
  });

  it('adopts the server duration when none was supplied', async () => {
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3], duration: 42.5 } });
    const { container } = render(WaveformPlayer, { props: { fileId: 'f1', duration: 0 } });

    await waitForMountedLoad();

    expect(canvasOf(container).getAttribute('aria-valuemax')).toBe('42.5');
  });

  it('leaves a caller-supplied duration alone when the server value is within 0.1s', async () => {
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3], duration: 10.05 } });
    const { container } = render(WaveformPlayer, { props: { fileId: 'f1', duration: 10 } });

    await waitForMountedLoad();

    expect(canvasOf(container).getAttribute('aria-valuemax')).toBe('10');
  });

  it('shows an error and keeps the canvas hidden when the server returns no samples', async () => {
    mockAxios.get.mockResolvedValue({ data: { waveform: [] } });
    const { container } = render(WaveformPlayer, { props: { fileId: 'f1' } });

    await waitForMountedLoad();

    expect(container.querySelector('.waveform-error')?.textContent).toContain('waveform.noData');
    expect(canvasOf(container).classList.contains('hidden')).toBe(true);
  });

  it('surfaces the request error message and retries on button click', async () => {
    mockAxios.get.mockRejectedValueOnce(new Error('network down'));
    const { container } = render(WaveformPlayer, { props: { fileId: 'f1' } });

    await waitForMountedLoad();
    expect(container.querySelector('.waveform-error')?.textContent).toContain('network down');
    expect(mockAxios.get).toHaveBeenCalledTimes(1);

    mockAxios.get.mockResolvedValueOnce({ data: { waveform: [1, 2, 3] } });
    const retryBtn = container.querySelector('.retry-button') as HTMLElement;
    await fireEvent.click(retryBtn);
    await waitForMountedLoad();

    expect(mockAxios.get).toHaveBeenCalledTimes(2);
    expect(container.querySelector('.waveform-error')).toBeNull();
  });
});

describe('click-to-seek', () => {
  it('dispatches seek with a time proportional to the click position', async () => {
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    const onSeek = vi.fn();
    const { container } = render(WaveformPlayer, {
      props: { fileId: 'f1', duration: 100 },
      events: { seek: onSeek },
    } as never);
    await waitForMountedLoad();

    const root = rootOf(container);
    vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      width: 300,
      top: 0,
      right: 300,
      bottom: 0,
      height: 0,
      x: 0,
      y: 0,
      toJSON() {},
    });
    const canvas = canvasOf(container);
    await fireEvent.click(canvas, { clientX: 75 }); // 25% across -> 25s of 100s

    expect(onSeek).toHaveBeenCalledWith(expect.objectContaining({ detail: { time: 25 } }));
  });

  it('queues a click seek that arrives before duration is known, and flushes it once duration arrives (#649)', async () => {
    // DEFECT THIS CATCHES: a click while `duration <= 0` used to be silently
    // discarded — no seek, no feedback, no memory of the click. That's
    // realistic on a fresh page load: the player hasn't reported its
    // duration to this component yet. The click should still take effect as
    // soon as duration becomes known, not require the user to click again.
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    const onSeek = vi.fn();
    const { container, rerender } = render(WaveformPlayer, {
      props: { fileId: 'f1', duration: 0 },
      events: { seek: onSeek },
    } as never);
    await waitForMountedLoad();

    const root = rootOf(container);
    vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      width: 300,
      top: 0,
      right: 300,
      bottom: 0,
      height: 0,
      x: 0,
      y: 0,
      toJSON() {},
    });

    await fireEvent.click(canvasOf(container), { clientX: 75 }); // 25% across
    expect(onSeek).not.toHaveBeenCalled(); // nothing to compute a time against yet

    await rerender({ fileId: 'f1', duration: 100 });

    expect(onSeek).toHaveBeenCalledWith(expect.objectContaining({ detail: { time: 25 } }));
  });

  it('does not replay a queued seek on an unrelated duration update once it has flushed', async () => {
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    const onSeek = vi.fn();
    const { container, rerender } = render(WaveformPlayer, {
      props: { fileId: 'f1', duration: 0 },
      events: { seek: onSeek },
    } as never);
    await waitForMountedLoad();

    const root = rootOf(container);
    vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      width: 300,
      top: 0,
      right: 300,
      bottom: 0,
      height: 0,
      x: 0,
      y: 0,
      toJSON() {},
    });

    await fireEvent.click(canvasOf(container), { clientX: 75 });
    await rerender({ fileId: 'f1', duration: 100 });
    expect(onSeek).toHaveBeenCalledTimes(1);

    onSeek.mockClear();
    await rerender({ fileId: 'f1', duration: 120 });
    expect(onSeek).not.toHaveBeenCalled();
  });

  it('dispatches exactly ONE seek for a real mouse click (mousedown + click)', async () => {
    // DEFECT THIS CATCHES (issue #645): the canvas had on:click AND on:mousedown
    // and both seeked, so every click on the waveform issued two identical seeks
    // to the media element — two Plyr seeks plus two raw seeks, four
    // `currentTime` assignments, per single click. Measured live before the fix.
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    const onSeek = vi.fn();
    const { container } = render(WaveformPlayer, {
      props: { fileId: 'f1', duration: 100 },
      events: { seek: onSeek },
    } as never);
    await waitForMountedLoad();

    const root = rootOf(container);
    vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      width: 300,
      top: 0,
      right: 300,
      bottom: 0,
      height: 0,
      x: 0,
      y: 0,
      toJSON() {},
    });

    const canvas = canvasOf(container);
    // A real pointer interaction fires mousedown, then mouseup, then click.
    await fireEvent.mouseDown(canvas, { clientX: 150 });
    await fireEvent.mouseUp(document);
    await fireEvent.click(canvas, { clientX: 150 });
    await nextAnimationFrame();

    expect(onSeek).toHaveBeenCalledTimes(1);
    expect(onSeek).toHaveBeenCalledWith(expect.objectContaining({ detail: { time: 50 } }));
  });

  it('still seeks on a synthetic click that has no preceding mousedown', async () => {
    // Assistive tech and programmatic activation dispatch `click` alone; the
    // dedupe above must not swallow those.
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    const onSeek = vi.fn();
    const { container } = render(WaveformPlayer, {
      props: { fileId: 'f1', duration: 100 },
      events: { seek: onSeek },
    } as never);
    await waitForMountedLoad();

    const root = rootOf(container);
    vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      width: 300,
      top: 0,
      right: 300,
      bottom: 0,
      height: 0,
      x: 0,
      y: 0,
      toJSON() {},
    });

    await fireEvent.click(canvasOf(container), { clientX: 75 });
    expect(onSeek).toHaveBeenCalledTimes(1);
    expect(onSeek).toHaveBeenCalledWith(expect.objectContaining({ detail: { time: 25 } }));
  });

  it('clamps a drag past the container edge to the end of the track, and tracks mousemove while dragging', async () => {
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    const onSeek = vi.fn();
    const { container } = render(WaveformPlayer, {
      props: { fileId: 'f1', duration: 100 },
      events: { seek: onSeek },
    } as never);
    await waitForMountedLoad();

    const root = rootOf(container);
    vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      width: 300,
      top: 0,
      right: 300,
      bottom: 0,
      height: 0,
      x: 0,
      y: 0,
      toJSON() {},
    });

    const canvas = canvasOf(container);
    await fireEvent.mouseDown(canvas, { clientX: 999 }); // past the edge, clamped to 1.0
    expect(onSeek).toHaveBeenCalledWith(expect.objectContaining({ detail: { time: 100 } }));

    onSeek.mockClear();
    await fireEvent.mouseMove(document, { clientX: 150 }); // 50% while still dragging
    // Drag seeks are coalesced to one per animation frame (issue #645): raw
    // mousemove fires far faster than a media element can service a seek, and
    // each uncoalesced seek can cost a fresh byte-range request.
    await nextAnimationFrame();
    expect(onSeek).toHaveBeenCalledWith(expect.objectContaining({ detail: { time: 50 } }));

    onSeek.mockClear();
    await fireEvent.mouseUp(document);
    await fireEvent.mouseMove(document, { clientX: 0 }); // no longer dragging
    await nextAnimationFrame();
    expect(onSeek).not.toHaveBeenCalled();
  });
});

describe('keyboard seek', () => {
  async function setup(currentTime = 20, duration = 100) {
    mockAxios.get.mockResolvedValue({ data: { waveform: [1, 2, 3] } });
    const onSeek = vi.fn();
    const { container } = render(WaveformPlayer, {
      props: { fileId: 'f1', duration, currentTime },
      events: { seek: onSeek },
    } as never);
    await waitForMountedLoad();
    return { container, onSeek };
  }

  it('steps back 1% of duration on ArrowLeft and prevents default', async () => {
    const { container, onSeek } = await setup(20, 100);
    const canvas = canvasOf(container);
    const event = await fireEvent.keyDown(canvas, { code: 'ArrowLeft' });

    expect(onSeek).toHaveBeenCalledWith(expect.objectContaining({ detail: { time: 19 } }));
    expect(event).toBe(false); // fireEvent returns false when preventDefault() was called
  });

  it('steps forward 1% of duration on ArrowRight, clamped to duration', async () => {
    const { container, onSeek } = await setup(99.7, 100);
    await fireEvent.keyDown(canvasOf(container), { code: 'ArrowRight' });

    expect(onSeek).toHaveBeenCalledWith(expect.objectContaining({ detail: { time: 100 } }));
  });

  it('jumps to 0 on Home and to duration on End', async () => {
    const { container, onSeek } = await setup(50, 100);
    await fireEvent.keyDown(canvasOf(container), { code: 'Home' });
    expect(onSeek).toHaveBeenLastCalledWith(expect.objectContaining({ detail: { time: 0 } }));

    await fireEvent.keyDown(canvasOf(container), { code: 'End' });
    expect(onSeek).toHaveBeenLastCalledWith(expect.objectContaining({ detail: { time: 100 } }));
  });

  it('ignores unrelated keys without dispatching', async () => {
    const { container, onSeek } = await setup(50, 100);
    await fireEvent.keyDown(canvasOf(container), { code: 'Tab' });
    expect(onSeek).not.toHaveBeenCalled();
  });

  it('does nothing when duration is not yet known', async () => {
    const { container, onSeek } = await setup(0, 0);
    await fireEvent.keyDown(canvasOf(container), { code: 'ArrowRight' });
    expect(onSeek).not.toHaveBeenCalled();
  });

  it('seeks to 0 on Home even when duration is not yet known', async () => {
    // 'Home' always means "seek to 0" — no `duration` is needed to compute
    // that target, so it doesn't need to wait for one like the other keys.
    const { container, onSeek } = await setup(0, 0);
    await fireEvent.keyDown(canvasOf(container), { code: 'Home' });
    expect(onSeek).toHaveBeenCalledWith(expect.objectContaining({ detail: { time: 0 } }));
  });
});
