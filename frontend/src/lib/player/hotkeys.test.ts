/**
 * Keyboard shortcuts (plan #1327 rows 30-31): the app's set, declared once for both players,
 * plus `L` toggling loop, which Video.js has no action for.
 * https://videojs.org/docs/framework/html/guides/keyboard-shortcuts
 */
import { afterEach, describe, expect, it } from 'vitest';
import { PLAYER_HOTKEYS, SEEK_STEP_SECONDS, attachLoopHotkey, playerHotkeys } from './hotkeys';
import { asMedia, fakeMedia } from './fakeVideojs';

afterEach(() => {
  document.body.innerHTML = '';
});

const bound = (keys: string) => PLAYER_HOTKEYS.filter((h) => h.keys === keys);

describe('PLAYER_HOTKEYS', () => {
  it('declares every shortcut the app had (row 31)', () => {
    expect(bound('Space')).toEqual([{ keys: 'Space', action: 'togglePaused' }]);
    expect(bound('k')).toEqual([{ keys: 'k', action: 'togglePaused' }]);
    expect(bound('0-9')).toEqual([{ keys: '0-9', action: 'seekToPercent' }]);
    expect(bound('m')).toEqual([{ keys: 'm', action: 'toggleMuted' }]);
    expect(bound('f')).toEqual([{ keys: 'f', action: 'toggleFullscreen' }]);
    expect(bound('c')).toEqual([{ keys: 'c', action: 'toggleSubtitles' }]);
  });

  it('seeks 10 s on the arrow keys and steps volume by 10% as before', () => {
    expect(SEEK_STEP_SECONDS).toBe(10);
    expect(bound('ArrowLeft')).toEqual([{ keys: 'ArrowLeft', action: 'seekStep', value: -10 }]);
    expect(bound('ArrowRight')).toEqual([{ keys: 'ArrowRight', action: 'seekStep', value: 10 }]);
    expect(bound('ArrowUp')).toEqual([{ keys: 'ArrowUp', action: 'volumeStep', value: 0.1 }]);
    expect(bound('ArrowDown')).toEqual([{ keys: 'ArrowDown', action: 'volumeStep', value: -0.1 }]);
  });

  it('adds the Video.js picture-in-picture and speed shortcuts', () => {
    expect(bound('i')).toEqual([{ keys: 'i', action: 'togglePictureInPicture' }]);
    expect(bound('>')).toEqual([{ keys: '>', action: 'speedUp' }]);
    expect(bound('<')).toEqual([{ keys: '<', action: 'speedDown' }]);
  });

  it('keeps L for loop: neither j nor l is bound to a library action (Q2)', () => {
    expect(PLAYER_HOTKEYS.filter((h) => /^[jl]$/i.test(h.keys))).toEqual([]);
  });

  it('declares each key pattern once', () => {
    const keys = PLAYER_HOTKEYS.map((h) => h.keys.toLowerCase());
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('scopes the bindings page-wide or to the player container', () => {
    expect(playerHotkeys('document').every((h) => h.target === 'document')).toBe(true);
    expect(playerHotkeys('player').every((h) => h.target === 'player')).toBe(true);
    expect(playerHotkeys('player')).toHaveLength(PLAYER_HOTKEYS.length);
  });
});

describe('attachLoopHotkey', () => {
  const press = (target: EventTarget, init: KeyboardEventInit) => {
    const e = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init });
    target.dispatchEvent(e);
    return e;
  };

  it('toggles media.loop on L and claims the key', () => {
    const media = fakeMedia();
    attachLoopHotkey(document, asMedia(media));
    const e = press(document.body, { key: 'l' });
    expect(media.loop).toBe(true);
    expect(e.defaultPrevented).toBe(true);
    press(document.body, { key: 'L' });
    expect(media.loop).toBe(false);
  });

  it.each([
    ['an input', '<input id="t" type="text">'],
    ['a textarea', '<textarea id="t"></textarea>'],
    ['a select', '<select id="t"><option>a</option></select>'],
    ['a contenteditable element', '<div contenteditable="true"><span id="t">x</span></div>'],
  ])('does nothing while typing in %s', (_name, html) => {
    document.body.innerHTML = html;
    const media = fakeMedia();
    attachLoopHotkey(document, asMedia(media));
    const e = press(document.getElementById('t')!, { key: 'l' });
    expect(media.loop).toBe(false);
    expect(e.defaultPrevented).toBe(false);
  });

  it('ignores modifier combinations and held-key repeats', () => {
    const media = fakeMedia();
    attachLoopHotkey(document, asMedia(media));
    press(document.body, { key: 'l', ctrlKey: true });
    press(document.body, { key: 'l', metaKey: true });
    press(document.body, { key: 'l', altKey: true });
    press(document.body, { key: 'L', shiftKey: true });
    press(document.body, { key: 'l', repeat: true });
    expect(media.loop).toBe(false);
  });

  it('listens only inside its target when scoped to the player container', () => {
    document.body.innerHTML = '<div id="player"><button id="in">x</button></div><p id="out">y</p>';
    const media = fakeMedia();
    attachLoopHotkey(document.getElementById('player')!, asMedia(media));
    press(document.getElementById('out')!, { key: 'l' });
    expect(media.loop).toBe(false);
    press(document.getElementById('in')!, { key: 'l' });
    expect(media.loop).toBe(true);
  });

  it('stops listening when disposed', () => {
    const media = fakeMedia();
    const dispose = attachLoopHotkey(document, asMedia(media));
    dispose();
    press(document.body, { key: 'l' });
    expect(media.loop).toBe(false);
  });
});
