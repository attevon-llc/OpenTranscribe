/**
 * The mini player's controls (issue #1327, plan Lane D), against the REAL Video.js 10 elements.
 *
 * jsdom upgrades the custom elements and runs their store, so labels, translations, the status
 * live region and the keyboard wiring are checked here without a browser. What jsdom cannot
 * do (actual playback, layout, the Popover top layer) is left to Playwright
 * (`backend/tests/e2e/test_media_playback.py`). Nothing is mocked: these must fail on any
 * player that lacks the capability, which is how they were watched red on the previous build.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { readFileSync } from 'fs';
import { resolve } from 'path';
import { getI18nTranslations } from '@videojs/html/i18n';
import { SUPPORTED_LANGUAGES } from '$lib/i18n/languages';
import { PLAYER_LOCALE_PACKS } from '$lib/player/i18n';
import { locale } from '$stores/locale';
import MiniPlayer from './MiniPlayer.svelte';

const COMPONENT_FILE = resolve(__dirname, 'MiniPlayer.svelte');

async function mount(props: Record<string, unknown> = {}) {
  const result = render(MiniPlayer, {
    props: {
      mediaUrl: 'blob:test-media',
      contentType: 'video/mp4',
      autoplay: false,
      fileId: '',
      ...props,
    },
  });
  await new Promise((r) => setTimeout(r, 0));
  const media = result.container.querySelector('video, audio') as HTMLMediaElement;
  return { ...result, media, ui: within(result.container) };
}

/** jsdom has no Fullscreen API; a test opts in to a browser that has one. */
const setElementFullscreen = (enabled: boolean) =>
  Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: enabled });

const fullscreenDescriptor = Object.getOwnPropertyDescriptor(
  Document.prototype,
  'fullscreenEnabled'
);

beforeEach(() => {
  locale.set('en');
});

afterEach(() => {
  locale.set('en');
  if (fullscreenDescriptor) {
    Object.defineProperty(Document.prototype, 'fullscreenEnabled', fullscreenDescriptor);
  }
  delete (document as { fullscreenEnabled?: boolean }).fullscreenEnabled;
  document.body.style.overflow = '';
});

describe('controls in the UI language (plan row 43)', () => {
  const translated = SUPPORTED_LANGUAGES.map((l) => l.code).filter((c) => c !== 'en');

  it('covers all eleven translated app locales', () => {
    expect(translated).toHaveLength(11);
  });

  it.each(translated)('names the play control in %s', async (code) => {
    await PLAYER_LOCALE_PACKS[code]?.();
    const label = getI18nTranslations(code)['buttons.play'];
    expect(label, `the ${code} pack has a play label`).toBeTruthy();
    expect(label).not.toBe('Play');

    locale.set(code);
    const { ui } = await mount();
    await waitFor(() => expect(ui.getByRole('button', { name: label })).toBeTruthy());
    expect(ui.queryByRole('button', { name: /^play$/i })).toBeNull();
  });

  it('switches with the app locale, with no remount', async () => {
    await PLAYER_LOCALE_PACKS.de?.();
    const { ui } = await mount();
    await waitFor(() => expect(ui.getByRole('button', { name: /^play$/i })).toBeTruthy());
    locale.set('de');
    const label = getI18nTranslations('de')['buttons.play'] as string;
    await waitFor(() => expect(ui.getByRole('button', { name: label })).toBeTruthy());
  });
});

describe('screen-reader announcements (new with Video.js)', () => {
  it('announces playback starting in a status live region inside the player', async () => {
    const { container, media } = await mount({ contentType: 'audio/mpeg' });
    const status = within(container).getByRole('status');
    let paused = true;
    Object.defineProperty(media, 'paused', { configurable: true, get: () => paused });
    paused = false;
    media.dispatchEvent(new Event('play'));
    media.dispatchEvent(new Event('playing'));
    const playing = getI18nTranslations('en')['status.playing'] ?? 'Playing';
    await waitFor(() => expect(status.textContent).toContain(playing));
  });
});

describe('RTL (plan row 44)', () => {
  // Physical left/right properties would keep the chrome left-to-right in Arabic; the sliders
  // pin their own axis with `direction: ltr`, by the library's design.
  const PHYSICAL =
    /(?:^|[\s;{])(?:left|right|(?:margin|padding|border)-(?:left|right)[\w-]*|text-align\s*:\s*(?:left|right)|float\s*:\s*(?:left|right))\s*:/m;

  it('lays out the controls with logical properties only', () => {
    const source = readFileSync(COMPONENT_FILE, 'utf8');
    const style = source.slice(source.indexOf('<style'));
    expect(style.length).toBeGreaterThan(100);
    expect(style.match(PHYSICAL)?.[0] ?? null).toBeNull();
    expect(PHYSICAL.test('.x { left: 0; }')).toBe(true);
    expect(PHYSICAL.test('.x { padding-right: 4px; }')).toBe(true);
    expect(PHYSICAL.test('.x { inset-inline-start: 0; }')).toBe(false);
  });
});

describe('keyboard: scoped to the player (plan rows 30-31)', () => {
  it('binds every shortcut to the player container, never the page', async () => {
    setElementFullscreen(true);
    const { container } = await mount();
    // Svelte sets these as element properties (they are declared props of <media-hotkey>).
    const hotkeys = [...container.querySelectorAll('media-hotkey')] as unknown as {
      action: string;
      target: string;
    }[];
    expect(hotkeys.length).toBeGreaterThan(8);
    expect(new Set(hotkeys.map((h) => h.target))).toEqual(new Set(['player']));
    const actions = hotkeys.map((h) => h.action);
    expect(actions).toEqual(
      expect.arrayContaining(['togglePaused', 'seekStep', 'toggleMuted', 'toggleFullscreen'])
    );
    expect(actions).not.toContain('toggleSubtitles');
  });

  it('toggles loop on L inside the player only', async () => {
    const { container, media } = await mount();
    const surface = container.querySelector('media-container') as HTMLElement;
    await fireEvent.keyDown(document.body, { key: 'l' });
    expect(media.loop).toBe(false);
    await fireEvent.keyDown(surface, { key: 'l' });
    expect(media.loop).toBe(true);
  });
});

describe('speed (plan rows 19-20)', () => {
  it('offers speeds up to 4x', async () => {
    const { container } = await mount({ contentType: 'audio/mpeg' });
    const player = container.querySelector('audio-player') as HTMLElement & {
      store: { state: { playbackRates: readonly number[] } };
    };
    await waitFor(() => expect(player.store.state.playbackRates).toContain(4));
    expect([...player.store.state.playbackRates]).toEqual([0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 4]);
  });
});

describe('audio vs video', () => {
  it('gives audio a volume slider and no fullscreen control', async () => {
    const { ui } = await mount({ contentType: 'audio/mpeg' });
    await waitFor(() => expect(ui.getByRole('slider', { name: /volume/i })).toBeTruthy());
    expect(ui.queryByRole('button', { name: /fullscreen/i })).toBeNull();
  });
});

describe('fullscreen and the full-window fallback (plan row 28)', () => {
  it('uses the library fullscreen button where the browser has the Fullscreen API', async () => {
    setElementFullscreen(true);
    const { container, ui } = await mount();
    expect(container.querySelector('media-fullscreen-button')).not.toBeNull();
    expect(ui.queryByRole('button', { name: /fullscreen/i, pressed: false })).toBeNull();
  });

  it('fills the window instead where it does not (iPhone), and leaves on Escape or F', async () => {
    setElementFullscreen(false);
    const { container, ui } = await mount();
    const root = container.querySelector('.mini-player') as HTMLElement;
    expect(container.querySelector('media-fullscreen-button')).toBeNull();

    await fireEvent.click(ui.getByRole('button', { name: /enter fullscreen/i }));
    expect(root.hasAttribute('data-full-window')).toBe(true);
    expect(document.body.style.overflow).toBe('hidden');
    expect(ui.getByRole('button', { name: /exit fullscreen/i }).getAttribute('aria-pressed')).toBe(
      'true'
    );

    await fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(root.hasAttribute('data-full-window')).toBe(false);
    expect(document.body.style.overflow).toBe('');

    const surface = container.querySelector('media-container') as HTMLElement;
    await fireEvent.keyDown(surface, { key: 'f' });
    expect(root.hasAttribute('data-full-window')).toBe(true);
  });
});

describe('mini player time readout', () => {
  it('draws one separator: the library already renders "/", so no CSS adds a second', async () => {
    const { container } = await mount();
    const separator = container.querySelector('.mini-time media-time-separator');
    expect(separator?.textContent?.trim()).toBe('/');
    const source = readFileSync(COMPONENT_FILE, 'utf8');
    expect(source).not.toMatch(/media-time-separator::(before|after)/);
  });

  it('keeps "elapsed / total" left to right in right-to-left languages', () => {
    const source = readFileSync(COMPONENT_FILE, 'utf8');
    const rule = source.match(/:global\(\.mini-time\)\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(rule).toMatch(/direction:\s*ltr/);
    expect(rule).toMatch(/unicode-bidi:\s*isolate/);
  });
});

describe('mini player speed values in right-to-left languages', () => {
  it('keeps the rate button and every rate label left to right', async () => {
    const { container } = await mount();
    const button = container.querySelector('media-playback-rate-button [data-part="value"]');
    expect(button?.getAttribute('dir')).toBe('ltr');
    const template = container.querySelector(
      'media-playback-rate-radio-group template'
    ) as HTMLTemplateElement | null;
    const label =
      template?.content.querySelector('[data-part="label"]') ??
      container.querySelector('media-playback-rate-radio-group [data-part="label"]');
    expect(label?.getAttribute('dir')).toBe('ltr');
  });
});
