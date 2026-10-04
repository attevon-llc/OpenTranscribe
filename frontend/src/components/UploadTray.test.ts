/**
 * Issue #752: five defects in the existing upload tray (UploadManager +
 * UploadProgress). Persistence is covered in stores/uploads.persistence.test.ts.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import fs from 'fs';
import path from 'path';
import UploadManager from './UploadManager.svelte';
import { uploadsStore } from '$stores/uploads';
import type { UploadItem, UploadEvent } from '$lib/services/uploadService';

const captured = vi.hoisted(() => ({ listener: null as ((e: UploadEvent) => void) | null }));
let current: UploadItem[] = [];

const mockUploadService = vi.hoisted(() => ({
  addEventListener: vi.fn((l: (e: UploadEvent) => void) => {
    captured.listener = l;
    return () => {};
  }),
  getAllUploads: vi.fn(),
  cancelUpload: vi.fn(),
  retryUpload: vi.fn(),
  removeUpload: vi.fn(),
  clearCompleted: vi.fn(),
  reset: vi.fn(),
}));
vi.mock('$lib/services/uploadService', () => ({ uploadService: mockUploadService }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

function makeUpload(overrides: Partial<UploadItem> = {}): UploadItem {
  return {
    id: 'u1',
    type: 'file',
    source: 'x',
    name: 'a.mp3',
    status: 'uploading',
    progress: 40,
    retryCount: 0,
    ...overrides,
  } as UploadItem;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  current = [];
  uploadsStore.reset();
  mockUploadService.getAllUploads.mockImplementation(() => current);
});

async function renderTray(upload: UploadItem = makeUpload()) {
  const result = render(UploadManager);
  current = [upload];
  captured.listener!({ type: 'added', uploadId: 'u1', data: upload });
  await tick();
  return result;
}

describe('UploadManager drag (defect 2)', () => {
  it('dragging toward the top-left moves the right/bottom-anchored tray the same way', async () => {
    const { container } = await renderTray();
    const root = container.querySelector('.upload-manager') as HTMLElement;
    expect(root.style.right).toBe('20px');
    expect(root.style.bottom).toBe('20px');

    const header = container.querySelector('.panel-header') as HTMLElement;
    await fireEvent.mouseDown(header, { clientX: 500, clientY: 500 });
    await fireEvent.mouseMove(document, { clientX: 450, clientY: 480 }); // 50 left, 20 up
    await fireEvent.mouseUp(document);

    // Moving the pointer left/up by (50, 20) must INCREASE right/bottom by the same.
    expect(root.style.right).toBe('70px');
    expect(root.style.bottom).toBe('40px');
  });
});

describe('UploadProgress (defects 1 and 5)', () => {
  it('fills the active bar from a theme token, not a hardcoded hex', async () => {
    const { container } = await renderTray();
    const fill = container.querySelector('.upload-item .progress-fill') as HTMLElement;
    expect(fill.getAttribute('style')).toContain('var(--primary-on-surface)');
    expect(fill.getAttribute('style')).not.toMatch(/#[0-9a-f]{3,6}/i);
  });

  it('uses tray-specific button classes that cannot collide with the gallery toolbar', async () => {
    const { container } = await renderTray(makeUpload({ status: 'failed', error: 'boom' }));
    expect(container.querySelector('.action-btn')).toBeNull();
    expect(container.querySelector('.cancel-btn')).toBeNull();
    expect(container.querySelectorAll('.upload-action-btn').length).toBeGreaterThan(0);
  });
});

describe('theme tokens (defects 1 and 3)', () => {
  const css = fs.readFileSync(path.resolve(__dirname, '../styles/theme.css'), 'utf8');
  const hex = (h: string): [number, number, number] =>
    [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) as [number, number, number];
  const lum = ([r, g, b]: [number, number, number]) => {
    const f = (c: number) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a: string, b: string) => {
    const [hi, lo] = [lum(hex(a)), lum(hex(b))].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  const token = (sel: string, name: string) =>
    css.slice(css.indexOf(sel)).match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`))![1];

  for (const [theme, sel] of [
    ['light', ':root'],
    ['dark', "[data-theme='dark']"],
  ] as const) {
    it(`${theme}: active progress fill clears 3:1 (WCAG 1.4.11) against its track`, () => {
      expect(
        ratio(token(sel, '--primary-on-surface'), token(sel, '--border-color'))
      ).toBeGreaterThanOrEqual(3);
    });
  }

  it('the tray uses the shared layer token instead of a raw z-index', () => {
    const src = fs.readFileSync(path.resolve(__dirname, 'UploadManager.svelte'), 'utf8');
    expect(src).not.toMatch(/z-index:\s*\d+/);
    expect(src).toContain('z-index: var(--z-toast)');
  });
});
