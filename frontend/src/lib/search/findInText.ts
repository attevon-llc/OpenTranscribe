/**
 * findInText — reusable literal (Ctrl+F-style) in-document find.
 *
 * Extracted from the matching loop that lived inline in TranscriptSearch, so the
 * summary find, the transcript find, and any future in-document search share one
 * tested implementation. Matching is literal case-insensitive substring — the
 * correct behavior for a "find in document" bar (browsers, editors, macOS all do
 * this) and the only kind that can be highlighted contiguously.
 *
 * This module is pure/side-effect free: components own their scroll/seek/highlight
 * side effects and drive them from the returned occurrences.
 */

export interface TextBlock {
  /** Stable id for the block (e.g. a segment uuid) used to scroll to a match. */
  id: string | number;
  /** The searchable text of the block. */
  text: string;
}

export interface TextOccurrence {
  /** Index of the block within the input array. */
  blockIndex: number;
  /** The block's stable id. */
  blockId: string | number;
  /** Character offset of the match within the block text. */
  start: number;
  /** Length of the matched substring (== query length). */
  length: number;
}

/**
 * Find every case-insensitive occurrence of `query` across `blocks`, in document
 * order. Returns an empty array for an empty/whitespace query.
 */
export function findOccurrences(blocks: TextBlock[], query: string): TextOccurrence[] {
  const needle = query.trim().toLowerCase();
  if (!needle || !blocks?.length) return [];

  const occurrences: TextOccurrence[] = [];
  for (let blockIndex = 0; blockIndex < blocks.length; blockIndex++) {
    const block = blocks[blockIndex];
    if (!block || typeof block.text !== 'string') continue;
    const haystack = block.text.toLowerCase();

    let from = 0;
    let at = haystack.indexOf(needle, from);
    while (at !== -1) {
      occurrences.push({
        blockIndex,
        blockId: block.id,
        start: at,
        length: needle.length,
      });
      from = at + needle.length;
      at = haystack.indexOf(needle, from);
    }
  }
  return occurrences;
}

export interface FindSnapshot {
  occurrences: TextOccurrence[];
  /** 0-based index of the active match, or -1 when there are none. */
  current: number;
  total: number;
  /** The active occurrence, or null. */
  currentOccurrence: TextOccurrence | null;
}

/**
 * Small framework-agnostic state machine wrapping `findOccurrences` with
 * wrap-around next/previous navigation. Each mutator returns a fresh snapshot so
 * Svelte components can assign it reactively.
 */
export function createFindState() {
  let occurrences: TextOccurrence[] = [];
  let current = -1;

  function snapshot(): FindSnapshot {
    return {
      occurrences,
      current,
      total: occurrences.length,
      currentOccurrence: current >= 0 ? occurrences[current] : null,
    };
  }

  return {
    snapshot,
    setQuery(blocks: TextBlock[], query: string): FindSnapshot {
      occurrences = findOccurrences(blocks, query);
      current = occurrences.length ? 0 : -1;
      return snapshot();
    },
    next(): FindSnapshot {
      if (occurrences.length) current = (current + 1) % occurrences.length;
      return snapshot();
    },
    previous(): FindSnapshot {
      if (occurrences.length) {
        current = (current - 1 + occurrences.length) % occurrences.length;
      }
      return snapshot();
    },
    clear(): FindSnapshot {
      occurrences = [];
      current = -1;
      return snapshot();
    },
  };
}

/** Next index with wrap-around (pure helper for consumers that manage their own state). */
export function nextIndex(current: number, total: number): number {
  if (total <= 0) return -1;
  return (current + 1) % total;
}

/** Previous index with wrap-around. */
export function previousIndex(current: number, total: number): number {
  if (total <= 0) return -1;
  return (current - 1 + total) % total;
}
