import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const imported = vi.hoisted(() => ({ modal: 0, shell: 0, section: vi.fn() }));

vi.mock('$components/SettingsModal.svelte', () => {
  imported.modal += 1;
  return { default: {} };
});
vi.mock('$components/settings/SettingsModalShell.svelte', () => {
  imported.shell += 1;
  return { default: {} };
});
vi.mock('$lib/settings/sectionLoaders', () => ({ prefetchSection: imported.section }));

import { prefetchSettingsModal, whenIdle } from './settingsModalLoader';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('prefetchSettingsModal', () => {
  it('warms the shell, the modal and the section the modal will land on', async () => {
    prefetchSettingsModal('download');
    await vi.waitFor(() => expect(imported.section).toHaveBeenCalledWith('download'));
    expect(imported.modal).toBe(1);
    expect(imported.shell).toBe(1);
  });
});

describe('whenIdle', () => {
  it('uses requestIdleCallback with a deadline', () => {
    const request = vi.fn();
    vi.stubGlobal('requestIdleCallback', request);
    const fn = vi.fn();

    whenIdle(fn);
    expect(request).toHaveBeenCalledWith(fn, { timeout: 4000 });
  });

  it('falls back to a timer where requestIdleCallback does not exist (Safari)', () => {
    vi.useFakeTimers();
    vi.stubGlobal('requestIdleCallback', undefined);
    const fn = vi.fn();

    whenIdle(fn);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1500);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe('first-open wiring', () => {
  const read = (path: string) => readFileSync(resolve(process.cwd(), path), 'utf8');

  it('prefetches from the user menu on hover and focus (pointerenter also fires for touch)', () => {
    const menu = read('src/components/navbar/UserDropdown.svelte');
    for (const event of ['on:pointerenter', 'on:focus']) {
      expect(menu).toMatch(new RegExp(`${event}[^=]*=\\{\\(\\) => prefetchSettingsModal\\(\\)\\}`));
    }
  });

  it('prefetches on idle after sign-in and shows the shell while the modal chunk loads', () => {
    const layout = read('src/routes/+layout.svelte');
    expect(layout).toMatch(/whenIdle\(\(\) => prefetchSettingsModal\(\)\)/);
    expect(layout).toMatch(/\{:else if SettingsModalShell && \$settingsModalStore\.isOpen\}/);
  });
});
