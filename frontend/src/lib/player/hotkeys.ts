/**
 * Keyboard shortcuts for both players, declared once (plan #1327 rows 30-31).
 *
 * `PLAYER_HOTKEYS` is rendered as `<media-hotkey>` declarations inside the player container:
 * https://videojs.org/docs/framework/html/guides/keyboard-shortcuts
 * https://videojs.org/docs/framework/html/reference/components/hotkey
 * The library skips unmodified shortcuts in editable fields and ignores held-key repeats of
 * toggles. It binds `j`/`l` to seek in its packaged skins; we do not (owner question Q2:
 * `L` keeps toggling loop as it did under Plyr, and the arrow keys already seek 10 s).
 * Video.js has no loop action, so `attachLoopHotkey` is the one shortcut we handle
 * ourselves, with the same typing safety.
 *
 * Scope: the file-detail player owns its page, so its bindings are page-wide
 * (`target="document"`, Plyr's `keyboard.global`); the floating mini player listens only on
 * its own container so it never fights the page underneath.
 */
import type { HotkeyProps } from '@videojs/html';

type PlayerHotkeyAction = HotkeyProps['action'];
export type PlayerHotkeyScope = NonNullable<HotkeyProps['target']>;

export interface PlayerHotkey {
  /** A `<media-hotkey keys>` pattern. */
  keys: string;
  action: PlayerHotkeyAction;
  value?: number;
}

/** Plyr's `seekTime` default, kept for the arrow keys. */
export const SEEK_STEP_SECONDS = 10;
/** Plyr stepped volume by 0.1 on Up/Down; the library default is 0.05. */
const VOLUME_STEP = 0.1;

export const PLAYER_HOTKEYS: readonly PlayerHotkey[] = Object.freeze([
  { keys: 'Space', action: 'togglePaused' },
  { keys: 'k', action: 'togglePaused' },
  { keys: 'ArrowLeft', action: 'seekStep', value: -SEEK_STEP_SECONDS },
  { keys: 'ArrowRight', action: 'seekStep', value: SEEK_STEP_SECONDS },
  { keys: 'ArrowUp', action: 'volumeStep', value: VOLUME_STEP },
  { keys: 'ArrowDown', action: 'volumeStep', value: -VOLUME_STEP },
  { keys: '0-9', action: 'seekToPercent' },
  { keys: 'm', action: 'toggleMuted' },
  { keys: 'f', action: 'toggleFullscreen' },
  { keys: 'c', action: 'toggleSubtitles' },
  { keys: 'i', action: 'togglePictureInPicture' },
  { keys: '>', action: 'speedUp' },
  { keys: '<', action: 'speedDown' },
] satisfies PlayerHotkey[]);

/** The table with each binding's `target` set for the player that renders it. */
export function playerHotkeys(
  scope: PlayerHotkeyScope
): (PlayerHotkey & { target: PlayerHotkeyScope })[] {
  return PLAYER_HOTKEYS.map((h) => ({ ...h, target: scope }));
}

/** Plyr's `selectors.editable`: where a bare key is typing, not a shortcut. */
const EDITABLE = 'input, textarea, select, [contenteditable]:not([contenteditable="false"])';

/** Whether a key event aimed at *target* is someone typing (the player must ignore it). */
export function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest(EDITABLE) !== null;
}

/**
 * Toggle `media.loop` on a bare `L` keydown inside *target* (the document for the main
 * player, the container for the mini player). Returns the cleanup function.
 */
export function attachLoopHotkey(target: EventTarget, media: HTMLMediaElement): () => void {
  const onKeyDown = (event: Event) => {
    if (!(event instanceof KeyboardEvent) || event.defaultPrevented || event.repeat) return;
    if (event.key.toLowerCase() !== 'l') return;
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    if (isEditableTarget(event.target)) return;
    event.preventDefault();
    media.loop = !media.loop;
  };
  target.addEventListener('keydown', onKeyDown);
  return () => target.removeEventListener('keydown', onKeyDown);
}
