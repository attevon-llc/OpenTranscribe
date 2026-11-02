/**
 * The optimistic "share with everyone" toggle shared by the prompt library and the LLM
 * configuration list: flip the flag at once, then put the previous state back if the server
 * refuses. Both steps are this one function; only the value differs.
 */
export interface Shareable {
  uuid: string;
  is_shared?: boolean;
  shared_at?: string;
}

/** A copy of `list` with one entry's share flag set, leaving the other entries untouched. */
export function withShareState<T extends Shareable>(
  list: T[],
  uuid: string,
  isShared: boolean,
  sharedAt: string | undefined
): T[] {
  return list.map((item) =>
    item.uuid === uuid ? { ...item, is_shared: isShared, shared_at: sharedAt } : item
  );
}
