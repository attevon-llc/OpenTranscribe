/** The two fields the comment list is ordered by. */
interface OrderableComment {
  timestamp: number | null;
  created_at: string;
}

/**
 * The comment list's one ordering rule: general comments (no timestamp, about the whole file)
 * first, oldest first; then timed comments in playback order, ties broken by creation time.
 */
function compareComments(a: OrderableComment, b: OrderableComment): number {
  const aGeneral = a.timestamp === null || a.timestamp === undefined;
  const bGeneral = b.timestamp === null || b.timestamp === undefined;
  if (aGeneral !== bGeneral) return aGeneral ? -1 : 1;
  if (!aGeneral && !bGeneral && a.timestamp !== b.timestamp) {
    return (a.timestamp as number) - (b.timestamp as number);
  }
  return a.created_at.localeCompare(b.created_at);
}

export function sortComments<T extends OrderableComment>(comments: T[]): T[] {
  return [...comments].sort(compareComments);
}
