/**
 * `UploadManager.svelte` had no test at all before #752, despite owning the
 * tray's positioning and z-index — exactly the two things that regressed
 * (issue plan §8.4/§8.5). Three behaviours are pinned here:
 *
 *  - No dragging: the previous drag math was inverted on both axes (moving
 *    right made the tray jump left on mouseup) and had no touch equivalent.
 *    #752 gap 3 removes it outright rather than fixing the math, so this
 *    proves there is no mousedown-driven repositioning left at all.
 *  - The tray sits in the documented `--z-toast` tier via the token, not a
 *    raw `9998` literal (#752 gap 4).
 *  - The tray is anchored with logical CSS properties (`inset-inline-end`,
 *    `inset-block-end`) rather than `right`/`bottom`, so it moves to the
 *    correct trailing corner in RTL locales (issue plan §2.6).
 *
 * The z-index/logical-property assertions read the compiled `<style>` block
 * directly (jsdom's `getComputedStyle` does not resolve custom properties
 * from a component's scoped stylesheet reliably), following the same
 * source-scan pattern already used by `styles/theme-parity.test.ts`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { writable, get } from 'svelte/store';
import fs from 'fs';
import path from 'path';

// `vi.mock` factories (and the `vi.hoisted` values they reference) run before
// this file's own top-level `import` statements are evaluated, so the
// factory below cannot close over `writable` from the `svelte/store` import
// above. Building the mock's store objects inside a single `vi.hoisted` call
// sidesteps that: everything the factory needs is created and returned from
// one hoisted block, with `svelte/store` imported dynamically inside it.
const stores = vi.hoisted(async () => {
  const { writable } = await import('svelte/store');
  const isExpandedStore = writable(false);
  const hasNewActivityStore = writable(false);
  const uploadCountStore = writable(1);
  const activeUploadCountStore = writable(0);
  const totalProgressStore = writable(0);
  const hasActiveUploadsStore = writable(false);
  const uploadStatsStore = writable({
    total: 1,
    active: 0,
    queued: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
  });
  const uploadsListStore = writable<{ uploads: unknown[] }>({ uploads: [] });
  const toggle = (await import('vitest')).vi.fn(() => isExpandedStore.update((v) => !v));
  const collapse = (await import('vitest')).vi.fn(() => isExpandedStore.set(false));
  const clearCompleted = (await import('vitest')).vi.fn();

  return {
    isExpandedStore,
    hasNewActivityStore,
    uploadCountStore,
    activeUploadCountStore,
    totalProgressStore,
    hasActiveUploadsStore,
    uploadStatsStore,
    uploadsListStore,
    toggle,
    collapse,
    clearCompleted,
  };
});

vi.mock('../stores/uploads', async () => {
  const s = await stores;
  return {
    uploadsStore: {
      subscribe: s.uploadsListStore.subscribe,
      toggle: s.toggle,
      collapse: s.collapse,
      clearCompleted: s.clearCompleted,
    },
    activeUploadCount: s.activeUploadCountStore,
    uploadCount: s.uploadCountStore,
    totalProgress: s.totalProgressStore,
    hasActiveUploads: s.hasActiveUploadsStore,
    isExpanded: s.isExpandedStore,
    hasNewActivity: s.hasNewActivityStore,
    uploadStats: s.uploadStatsStore,
  };
});

vi.mock('../stores/locale', () => ({
  t: {
    subscribe: (run: (fn: (key: string) => string) => void) => {
      run((key) => key);
      return () => {};
    },
  },
}));

import UploadManager from './UploadManager.svelte';

const SOURCE = fs.readFileSync(path.resolve(__dirname, 'UploadManager.svelte'), 'utf8');

const { isExpandedStore, uploadCountStore, toggle } = await stores;

beforeEach(() => {
  vi.clearAllMocks();
  isExpandedStore.set(false);
  uploadCountStore.set(1);
});

describe('UploadManager — no dragging (#752 gap 3)', () => {
  it('has no mousedown handler anywhere in the markup', () => {
    expect(SOURCE).not.toMatch(/on:mousedown/);
  });

  it('defines no drag-related state, math, or event listeners', () => {
    for (const token of [
      'isDragging',
      'dragOffset',
      'handleDragStart',
      'handleDragMove',
      'handleDragEnd',
      'upload-manager-position',
    ]) {
      expect(SOURCE).not.toContain(token);
    }
  });

  it('the collapsed badge is a plain click target, not "grab" cursor', () => {
    expect(SOURCE).not.toMatch(/cursor:\s*grab/);
  });
});

describe('UploadManager — z-index uses the --z-toast token, not a raw literal (#752 gap 4)', () => {
  it('does not hardcode 9998', () => {
    expect(SOURCE).not.toMatch(/z-index:\s*9998/);
  });

  it('uses var(--z-toast)', () => {
    expect(SOURCE).toMatch(/z-index:\s*var\(--z-toast\)/);
  });
});

describe('UploadManager — anchored with logical CSS properties for RTL (issue plan §2.6)', () => {
  it('does not anchor with physical right/bottom on the tray container', () => {
    const trayRule = SOURCE.slice(
      SOURCE.indexOf('.upload-manager {'),
      SOURCE.indexOf('.upload-manager {') + 400
    );
    expect(trayRule).not.toMatch(/[^-]\bright:/);
    expect(trayRule).not.toMatch(/[^-]\bbottom:/);
  });

  it('uses inset-inline-end / inset-block-end instead', () => {
    expect(SOURCE).toMatch(/inset-inline-end:\s*20px/);
    expect(SOURCE).toMatch(/inset-block-end:\s*20px/);
  });
});

describe('UploadManager — collapse/expand still works without drag', () => {
  it('toggle() is called when the collapsed badge is clicked', async () => {
    isExpandedStore.set(false);
    const { getByRole } = render(UploadManager);

    await fireEvent.click(getByRole('button'));

    expect(toggle).toHaveBeenCalledTimes(1);
  });

  it('renders the expanded panel when isExpanded is true', () => {
    isExpandedStore.set(true);
    const { container } = render(UploadManager);

    expect(container.querySelector('.upload-panel')).not.toBeNull();
    expect(get(isExpandedStore)).toBe(true);
  });
});
