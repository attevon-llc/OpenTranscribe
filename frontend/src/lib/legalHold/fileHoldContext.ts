/**
 * Whether the file on the page is frozen by a legal hold, shared through Svelte context.
 *
 * The file page sets it once from `file.hold.held`; every edit control under it (title,
 * transcript segments, speakers, tags, collections, summary, comments, reprocess) reads it
 * to render disabled with the visible reason, without threading a prop through each layer.
 * A component rendered outside the file page (the search transcript modal, a test) gets a
 * store that is always false, so nothing outside the page is ever shown frozen.
 */
import { getContext, setContext } from 'svelte';
import { readable, writable, type Readable, type Writable } from 'svelte/store';

const FILE_HOLD_KEY = Symbol('fileHoldFrozen');
const NEVER_FROZEN: Readable<boolean> = readable(false);

function setFileFrozenContext(frozen: Readable<boolean>): void {
  setContext(FILE_HOLD_KEY, frozen);
}

/** The page's side: a store the page sets from `file.hold.held`, provided to every child. */
export function provideFileFrozen(): Writable<boolean> {
  const frozen = writable(false);
  setFileFrozenContext(frozen);
  return frozen;
}

/** Call during component initialisation. */
export function fileFrozen(): Readable<boolean> {
  return getContext<Readable<boolean> | undefined>(FILE_HOLD_KEY) ?? NEVER_FROZEN;
}
