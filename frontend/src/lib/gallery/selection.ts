/**
 * Selection arithmetic for the gallery grid: toggling a card and shift-click range selection.
 * Pure over a `Set` of file uuids, so the store only decides when to apply it.
 */

/** A copy of `selected` with `fileId` added, or removed when it was already in. */
export function toggled(selected: ReadonlySet<string>, fileId: string): Set<string> {
  const next = new Set(selected);
  if (next.has(fileId)) {
    next.delete(fileId);
  } else {
    next.add(fileId);
  }
  return next;
}

/**
 * The selection after a click on `fileId`. Shift-click with an anchor selects the whole range
 * between the anchor and the target in list order; a plain click, a shift-click with no anchor,
 * or an anchor/target no longer in the list toggles just the one file.
 */
export function selectionAfterClick(
  selected: ReadonlySet<string>,
  fileIds: readonly string[],
  anchorId: string | null,
  fileId: string,
  shiftKey: boolean
): Set<string> {
  if (!shiftKey || !anchorId) return toggled(selected, fileId);
  const anchorIdx = fileIds.indexOf(anchorId);
  const targetIdx = fileIds.indexOf(fileId);
  if (anchorIdx === -1 || targetIdx === -1) return toggled(selected, fileId);

  const next = new Set(selected);
  const start = Math.min(anchorIdx, targetIdx);
  const end = Math.max(anchorIdx, targetIdx);
  for (let i = start; i <= end; i++) {
    next.add(fileIds[i]);
  }
  return next;
}
