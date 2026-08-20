/**
 * Resolves a backend `key_path` (e.g. `"major_topics[0].key_points[2]"`) against an
 * already-loaded `summary_data` object.
 *
 * Mirrors `backend/app/services/search/summary_search.py::_get_by_path` exactly, so a
 * path computed server-side against a summary (issue #462's `GET /search
 * ?result_type=summaries`) always resolves here too — the backend masks in place and
 * preserves the container shape, so a path collected from its tree resolves on the
 * frontend's copy of the same (masked) tree.
 *
 * This exists only to locate WHERE to scroll inside `SummaryModal`'s existing
 * find/highlight machinery (`searchQuery` + `currentMatchIndex`) — it does not render
 * or highlight anything itself.
 */
export function resolveSummaryKeyPath(data: unknown, keyPath: string): string | null {
  if (!keyPath) return null;

  const parts = keyPath.match(/[^.[\]]+|\[\d+\]/g);
  if (!parts) return null;

  let current: unknown = data;
  for (const part of parts) {
    if (current === null || current === undefined) return null;

    if (part.startsWith('[')) {
      const index = Number(part.slice(1, -1));
      if (!Array.isArray(current) || Number.isNaN(index)) return null;
      current = current[index];
    } else {
      if (typeof current !== 'object' || Array.isArray(current)) return null;
      current = (current as Record<string, unknown>)[part];
    }
  }

  return typeof current === 'string' ? current : null;
}
