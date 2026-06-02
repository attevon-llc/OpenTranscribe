<script lang="ts">
  import { createEventDispatcher, tick, onDestroy } from 'svelte';
  import { getSpeakerColorForSegment } from '$lib/utils/speakerColors';
  import { translateSpeakerLabel } from '$lib/i18n';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import Spinner from '../ui/Spinner.svelte';
  import BaseModal from '../ui/BaseModal.svelte';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import { formatClock } from '$lib/utils/formatting';
  import type { SearchOccurrence } from '$stores/search';

  export let isOpen: boolean = false;
  export let fileUuid: string = '';
  export let fileName: string = '';
  export let searchQuery: string = '';
  export let occurrences: SearchOccurrence[] = [];

  const dispatch = createEventDispatcher<{ close: void }>();

  interface GroupedSegment {
    speakerName: string;
    speaker_label: string;
    text: string;
    startTime: number;
    endTime: number;
    isKeyword: boolean;
    isSemantic: boolean;
    highlightedText: string;
    segmentIndex: number;
    rawStartIndex: number; // Index of the first raw segment in this group
  }

  interface MatchPosition {
    type: 'keyword' | 'semantic';
    segmentIndex: number;      // Resolved AFTER segments load (-1 if unresolved)
    startTime: number;         // From occurrence
    endTime: number;           // From occurrence
    resolved: boolean;         // Whether segmentIndex has been resolved
  }

  let loading = false;
  let error: string | null = null;
  let groupedSegments: GroupedSegment[] = [];
  let matchPositions: MatchPosition[] = [];
  let allMatchPositions: MatchPosition[] = []; // All matches including unloaded
  let currentMatchIdx = 0;
  let contentElement: HTMLElement | null = null;
  let navigatingToMatch = false; // Loading indicator for navigation

  // Pre-computed query terms for efficient highlighting (computed once per search)
  let cachedQueryWords: string[] = [];
  let cachedQueryStems: string[] = [];
  let cachedQueryPrefixes: string[] = [];

  // Progressive loading state
  const SEGMENTS_PER_PAGE = 200;
  let totalSegmentCount = 0;
  let totalSpeakerSegmentCount = 0;
  let loadedSegmentOffset = 0;
  let hasMoreSegments = false;
  let loadingMoreSegments = false;
  let infiniteScrollSentinel: HTMLElement | null = null;
  let infiniteScrollObserver: IntersectionObserver | null = null;
  let scrollProgress = 0;

  // Build time ranges from occurrences
  function buildTimeRanges() {
    const keywordRanges: { start: number; end: number }[] = [];
    const semanticRanges: { start: number; end: number }[] = [];

    for (const occ of occurrences) {
      const range = { start: occ.start_time, end: occ.end_time };
      if (occ.has_keyword_match) {
        keywordRanges.push(range);
      } else {
        semanticRanges.push(range);
      }
    }
    return { keywordRanges, semanticRanges };
  }

  function overlapsAny(start: number, end: number, ranges: { start: number; end: number }[]): boolean {
    return ranges.some(r => start < r.end && end > r.start);
  }

  function escapeHtml(text: string): string {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#x27;');
  }

  /**
   * Get a simple word stem by removing common suffixes.
   * Enables matching semantic variations like china→chinese, economy→economic.
   */
  function getWordStem(word: string): string {
    const w = word.toLowerCase();
    // Common suffixes to strip (order matters - check longer ones first)
    const suffixes = [
      "ization", "isation", "ational", "ousness", "iveness", "fulness",
      "ically", "iously", "lessly", "ations", "encies", "ancies",
      "ingly", "ously", "ively", "fully", "ation", "ition", "ement",
      "ness", "ment", "ence", "ance", "able", "ible", "ally", "ious",
      "eous", "ical", "less", "ness", "ship", "ward", "wise", "like",
      "ing", "ies", "ied", "ian", "ese", "ish", "ive", "ous", "ful",
      "ant", "ent", "ion", "ism", "ist", "ity", "ory", "ary", "ery",
      "ing", "ed", "er", "ly", "al", "en", "es", "ty", "ry", "ic", "s"
    ];
    for (const suffix of suffixes) {
      if (w.length > suffix.length + 2 && w.endsWith(suffix)) {
        return w.slice(0, -suffix.length);
      }
    }
    return w;
  }

  // Pre-compute query stems and prefixes when searchQuery changes (runs once per search)
  $: {
    const words = searchQuery.toLowerCase().split(/\s+/).filter(w => w.length >= 3);
    cachedQueryWords = words;
    cachedQueryStems = words.map(getWordStem);
    cachedQueryPrefixes = words
      .filter(w => w.length >= 4)
      .map(w => w.slice(0, Math.max(4, w.length - 2)));
  }

  /**
   * Check if a word should be highlighted based on semantic similarity to query terms.
   * Uses stem matching and prefix matching for semantic variations.
   */
  function shouldHighlightWord(word: string, queryWords: string[], queryStems: string[], queryPrefixes: string[]): boolean {
    const wordLower = word.toLowerCase();
    const wordStem = getWordStem(wordLower);

    // Check exact match
    if (queryWords.includes(wordLower)) return true;

    // Check stem match (china→chin matches chinese→chin)
    if (queryStems.includes(wordStem)) return true;

    // Check prefix match
    for (const prefix of queryPrefixes) {
      if (wordLower.startsWith(prefix) || wordStem.startsWith(prefix)) return true;
    }

    // Check if query word is prefix of this word
    for (const qw of queryWords) {
      if (wordLower.startsWith(qw) || wordStem.startsWith(getWordStem(qw))) return true;
    }

    return false;
  }

  function highlightQueryTerms(text: string, cssClass: string): string {
    // Use pre-computed cached values for efficiency
    if (cachedQueryWords.length === 0) return escapeHtml(text);

    // Process text word by word, preserving non-word characters
    const result: string[] = [];
    let currentPos = 0;
    const wordPattern = /\b([a-zA-Z]+)\b/g;
    let match: RegExpExecArray | null;

    while ((match = wordPattern.exec(text)) !== null) {
      // Add text before this word (escaped)
      if (match.index > currentPos) {
        result.push(escapeHtml(text.substring(currentPos, match.index)));
      }

      const word = match[1];
      if (shouldHighlightWord(word, cachedQueryWords, cachedQueryStems, cachedQueryPrefixes)) {
        result.push(`<span class="${cssClass}">${escapeHtml(word)}</span>`);
      } else {
        result.push(escapeHtml(word));
      }

      currentPos = match.index + match[0].length;
    }

    // Add remaining text (escaped)
    if (currentPos < text.length) {
      result.push(escapeHtml(text.substring(currentPos)));
    }

    return result.join('');
  }

  /**
   * Per-raw-segment text entry with its match classification.
   * Used to build inline highlights within grouped speaker blocks.
   */
  interface TextEntry {
    text: string;
    isKeyword: boolean;
    isSemantic: boolean;
  }

  /**
   * Process raw segments into grouped segments with per-segment inline highlighting.
   * Groups by speaker (merging consecutive same-speaker segments), but highlights
   * only the specific raw segments that overlap keyword/semantic time ranges.
   */
  function processSegments(
    segments: any[],
    keywordRanges: { start: number; end: number }[],
    semanticRanges: { start: number; end: number }[],
    startSegIdx: number,
    existingLastGroup: GroupedSegment | null,
    rawStartOffset: number = 0
  ): { grouped: GroupedSegment[]; nextSegIdx: number } {
    const sorted = [...segments].sort((a: any, b: any) => {
      return parseFloat(String(a.start_time || 0)) - parseFloat(String(b.start_time || 0));
    });

    const grouped: GroupedSegment[] = [];
    let currentSpeaker: string | null = existingLastGroup ? existingLastGroup.speakerName : null;
    let currentLabel: string | null = existingLastGroup ? existingLastGroup.speaker_label : null;
    let currentEntries: TextEntry[] = [];
    let currentStart = existingLastGroup ? existingLastGroup.startTime : 0;
    let currentEnd = existingLastGroup ? existingLastGroup.endTime : 0;
    let segIdx = startSegIdx;
    let currentRawStartIndex = existingLastGroup ? existingLastGroup.rawStartIndex : rawStartOffset;

    function buildHighlightedText(entries: TextEntry[]): string {
      return entries.map(entry => {
        if (entry.isKeyword) {
          // For keyword matches, highlight the specific matching words
          return highlightQueryTerms(entry.text, 'search-keyword-match');
        } else if (entry.isSemantic) {
          // For semantic matches, highlight the ENTIRE segment as one unit
          // The whole segment is semantically relevant, not just specific words
          return `<span class="search-semantic-segment">${escapeHtml(entry.text)}</span>`;
        } else {
          return escapeHtml(entry.text);
        }
      }).join(' ');
    }

    function flushGroup() {
      if (currentSpeaker && currentEntries.length > 0) {
        const hasKeyword = currentEntries.some(e => e.isKeyword);
        const hasSemantic = currentEntries.some(e => e.isSemantic);

        grouped.push({
          speakerName: currentSpeaker,
          speaker_label: currentLabel || 'Unknown',
          text: currentEntries.map(e => e.text).join(' '),
          startTime: currentStart,
          endTime: currentEnd,
          isKeyword: hasKeyword,
          isSemantic: hasSemantic && !hasKeyword,
          highlightedText: buildHighlightedText(currentEntries),
          segmentIndex: segIdx,
          rawStartIndex: currentRawStartIndex,
        });
        segIdx++;
      }
    }

    sorted.forEach((segment: any, rawIdx: number) => {
      // Backend guarantees `resolved_speaker_name`; prefer it directly, with a
      // last-resort guard for old payloads. `speakerLabel` stays for color mapping.
      const speakerName = segment.resolved_speaker_name || 'Unknown Speaker';
      const speakerLabel = segment.speaker_label || segment.speaker?.name || 'Unknown';
      const startTime = parseFloat(String(segment.start_time || 0));
      const endTime = parseFloat(String(segment.end_time || 0));

      // Classify this individual raw segment
      const segIsKw = overlapsAny(startTime, endTime, keywordRanges);
      const segIsSem = !segIsKw && overlapsAny(startTime, endTime, semanticRanges);

      const entry: TextEntry = {
        text: segment.text || '',
        isKeyword: segIsKw,
        isSemantic: segIsSem
      };

      // Group by speaker only — highlight granularity is per raw segment
      if (speakerName !== currentSpeaker) {
        flushGroup();
        currentSpeaker = speakerName;
        currentLabel = speakerLabel;
        currentEntries = [entry];
        currentStart = startTime;
        currentEnd = endTime;
        currentRawStartIndex = rawStartOffset + rawIdx;
      } else {
        currentEntries.push(entry);
        currentEnd = endTime;
      }
    });
    flushGroup();

    return {
      grouped,
      nextSegIdx: segIdx
    };
  }

  /** Build ALL match positions from occurrences (pre-known from search) */
  function buildAllMatchPositionsFromOccurrences(): MatchPosition[] {
    const positions: MatchPosition[] = [];
    for (const occ of occurrences) {
      const type = occ.has_keyword_match ? 'keyword' : 'semantic';
      positions.push({
        type,
        segmentIndex: -1,        // Unresolved initially - will be resolved when segments load
        startTime: occ.start_time,
        endTime: occ.end_time,
        resolved: false
      });
    }
    // Sort by time for chronological navigation
    return positions.sort((a, b) => a.startTime - b.startTime);
  }

  /** Find the grouped segment that contains this time range */
  function resolveMatchPositionIndex(pos: MatchPosition): number {
    for (const seg of groupedSegments) {
      // Check if the occurrence's time range overlaps with this segment
      if (pos.startTime < seg.endTime && pos.endTime > seg.startTime) {
        return seg.segmentIndex;
      }
    }
    return -1; // Not found in loaded segments
  }

  /** Wait for a segment element to exist in the DOM */
  async function waitForSegmentInDOM(targetSegmentIndex: number, maxWaitMs: number = 3000): Promise<boolean> {
    const startTime = Date.now();

    while (Date.now() - startTime < maxWaitMs) {
      const element = contentElement?.querySelector(`[data-seg-index="${targetSegmentIndex}"]`);
      if (element) {
        return true;
      }
      await new Promise(resolve => requestAnimationFrame(resolve));
    }

    return false;
  }

  /** Build match positions from grouped segments (used for matchPositions local tracking) */
  function buildMatchPositions(segments: GroupedSegment[]): MatchPosition[] {
    const positions: MatchPosition[] = [];
    for (const seg of segments) {
      if (seg.isKeyword) {
        const words = searchQuery.toLowerCase().split(/\s+/).filter((w: string) => w.length >= 3);
        if (words.length > 0) {
          const patterns = words.map((w: string) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
          const regex = new RegExp(`\\b(${patterns.join('|')})\\b`, 'gi');
          const matches = seg.text.match(regex);
          const count = matches ? matches.length : 0;
          for (let i = 0; i < Math.max(1, count); i++) {
            positions.push({
              type: 'keyword',
              segmentIndex: seg.segmentIndex,
              startTime: seg.startTime,
              endTime: seg.endTime,
              resolved: true
            });
          }
        } else {
          positions.push({
            type: 'keyword',
            segmentIndex: seg.segmentIndex,
            startTime: seg.startTime,
            endTime: seg.endTime,
            resolved: true
          });
        }
      } else if (seg.isSemantic) {
        positions.push({
          type: 'semantic',
          segmentIndex: seg.segmentIndex,
          startTime: seg.startTime,
          endTime: seg.endTime,
          resolved: true
        });
      }
    }
    return positions;
  }

  // Track the next segment index for grouping continuity
  let nextSegIdx = 0;

  async function loadTranscript() {
    if (!fileUuid) return;
    loading = true;
    error = null;
    groupedSegments = [];
    matchPositions = [];
    allMatchPositions = buildAllMatchPositionsFromOccurrences(); // Pre-build from ALL occurrences
    currentMatchIdx = 0;
    totalSegmentCount = 0;
    totalSpeakerSegmentCount = 0;
    loadedSegmentOffset = 0;
    hasMoreSegments = false;
    loadingMoreSegments = false;
    nextSegIdx = 0;

    try {
      const res = await axiosInstance.get(`/files/${fileUuid}`, {
        params: {
          segment_limit: SEGMENTS_PER_PAGE,
          segment_offset: 0
        }
      });

      const segments = res.data.transcript_segments || [];
      if (segments.length === 0) {
        loading = false;
        return;
      }

      // Track total and loaded counts
      totalSegmentCount = res.data.total_segments || segments.length;
      totalSpeakerSegmentCount = res.data.total_speaker_segments || 0;
      loadedSegmentOffset = segments.length;
      hasMoreSegments = loadedSegmentOffset < totalSegmentCount;

      // Group consecutive same-speaker segments
      const { keywordRanges, semanticRanges } = buildTimeRanges();
      const result = processSegments(segments, keywordRanges, semanticRanges, 0, null, 0);
      nextSegIdx = result.nextSegIdx;

      groupedSegments = result.grouped;
      matchPositions = buildMatchPositions(result.grouped);
      currentMatchIdx = 0;

      // Resolve segment indices for all matches that are now loaded
      for (const pos of allMatchPositions) {
        if (!pos.resolved) {
          const resolvedIdx = resolveMatchPositionIndex(pos);
          if (resolvedIdx !== -1) {
            pos.segmentIndex = resolvedIdx;
            pos.resolved = true;
          }
        }
      }

      loading = false;

      // Auto-scroll to first match
      await tick();
      scrollToCurrentMatch();

      // Set up infinite scroll for additional pages
      setupInfiniteScrollObserver();
    } catch (e: any) {
      console.error('Failed to load transcript:', e);
      error = e?.response?.data?.detail || 'Failed to load transcript';
      loading = false;
    }
  }

  async function loadMoreSegments() {
    if (loadingMoreSegments || !hasMoreSegments || !fileUuid) return;
    loadingMoreSegments = true;

    try {
      const res = await axiosInstance.get(`/files/${fileUuid}`, {
        params: {
          segment_limit: SEGMENTS_PER_PAGE,
          segment_offset: loadedSegmentOffset
        }
      });

      const newSegments = res.data.transcript_segments || [];
      if (newSegments.length === 0) {
        hasMoreSegments = false;
        loadingMoreSegments = false;
        return;
      }

      const { keywordRanges, semanticRanges } = buildTimeRanges();

      // Check if we should try to continue the last group from previous batch
      const lastExisting = groupedSegments.length > 0 ? groupedSegments[groupedSegments.length - 1] : null;
      const result = processSegments(newSegments, keywordRanges, semanticRanges, nextSegIdx, null, loadedSegmentOffset);
      nextSegIdx = result.nextSegIdx;

      // If the first new group has the same speaker as the last existing group,
      // merge them for speaker continuity (inline highlights are preserved)
      const firstNew = result.grouped.length > 0 ? result.grouped[0] : null;
      const canMerge = lastExisting && firstNew
        && firstNew.speakerName === lastExisting.speakerName;

      if (canMerge && lastExisting && firstNew) {
        const mergedGroup: GroupedSegment = {
          ...lastExisting,
          text: lastExisting.text + ' ' + firstNew.text,
          endTime: firstNew.endTime,
          isKeyword: lastExisting.isKeyword || firstNew.isKeyword,
          isSemantic: lastExisting.isSemantic || firstNew.isSemantic,
          highlightedText: lastExisting.highlightedText + ' ' + firstNew.highlightedText
        };

        // Replace last group and append the rest
        groupedSegments = [...groupedSegments.slice(0, -1), mergedGroup, ...result.grouped.slice(1)];
      } else {
        groupedSegments = [...groupedSegments, ...result.grouped];
      }

      loadedSegmentOffset += newSegments.length;
      hasMoreSegments = loadedSegmentOffset < totalSegmentCount;

      // Rebuild match positions for currently loaded segments (for scrolling)
      matchPositions = buildMatchPositions(groupedSegments);

      // Resolve segment indices for any newly covered matches
      for (const pos of allMatchPositions) {
        if (!pos.resolved) {
          const resolvedIdx = resolveMatchPositionIndex(pos);
          if (resolvedIdx !== -1) {
            pos.segmentIndex = resolvedIdx;
            pos.resolved = true;
          }
        }
      }
    } catch (e: any) {
      console.error('Failed to load more segments:', e);
    }
    loadingMoreSegments = false;
  }

  function setupInfiniteScrollObserver() {
    if (typeof IntersectionObserver !== 'undefined' && !infiniteScrollObserver) {
      infiniteScrollObserver = new IntersectionObserver(
        (entries) => {
          const entry = entries[0];
          if (entry?.isIntersecting && hasMoreSegments && !loadingMoreSegments) {
            loadMoreSegments();
          }
        },
        { rootMargin: '200px' }
      );
    }
    // Observe the sentinel if it exists
    if (infiniteScrollSentinel && infiniteScrollObserver) {
      infiniteScrollObserver.observe(infiniteScrollSentinel);
    }
  }

  // Handle scroll to update reading progress.
  // Uses the first visible block index against the backend-precomputed total so
  // progress stays stable as more segments load via infinite scroll.
  function handleContentScroll(event: Event) {
    const target = event.target as HTMLElement;
    if (!target) return;

    const total = totalSpeakerSegmentCount || groupedSegments.length;
    if (total === 0) return;

    const blockElements = target.querySelectorAll('[data-seg-index]');
    if (blockElements.length === 0) { scrollProgress = 0; return; }

    // Only snap to 100% when truly at the end (no more segments to load)
    const atBottom = Math.abs(target.scrollHeight - target.clientHeight - target.scrollTop) < 2;
    if (atBottom && !hasMoreSegments) { scrollProgress = 100; return; }

    const viewportTop = target.scrollTop;
    let firstVisibleIdx = 0;
    for (let i = 0; i < blockElements.length; i++) {
      const el = blockElements[i] as HTMLElement;
      if (el.offsetTop - target.offsetTop >= viewportTop) { firstVisibleIdx = i; break; }
    }

    const newProgress = Math.min(99, Math.round((firstVisibleIdx / total) * 100));
    // Never decrease progress while loading more segments (prevents glitch on infinite scroll)
    if (loadingMoreSegments && newProgress < scrollProgress) return;
    scrollProgress = newProgress;
  }

  // Observe the sentinel element when it's available
  $: if (infiniteScrollSentinel && infiniteScrollObserver) {
    infiniteScrollObserver.observe(infiniteScrollSentinel);
  }

  // Load transcript when modal opens
  $: if (isOpen && fileUuid) {
    loadTranscript();
  }

  // Reset when modal closes
  $: if (!isOpen) {
    groupedSegments = [];
    matchPositions = [];
    currentMatchIdx = 0;
    error = null;
    hasMoreSegments = false;
    loadingMoreSegments = false;
    loadedSegmentOffset = 0;
    totalSegmentCount = 0;
    totalSpeakerSegmentCount = 0;
    scrollProgress = 0;
    nextSegIdx = 0;
    if (infiniteScrollObserver) {
      infiniteScrollObserver.disconnect();
      infiniteScrollObserver = null;
    }
  }

  onDestroy(() => {
    if (infiniteScrollObserver) {
      infiniteScrollObserver.disconnect();
      infiniteScrollObserver = null;
    }
  });

  function scrollToCurrentMatch() {
    if (allMatchPositions.length === 0) return;

    requestAnimationFrame(() => {
      if (!contentElement) return;
      const pos = allMatchPositions[currentMatchIdx];
      if (!pos || pos.segmentIndex === -1) return;

      // Find the segment element
      const segEl = contentElement.querySelector(`[data-seg-index="${pos.segmentIndex}"]`) as HTMLElement;
      if (!segEl) return;

      // Find highlighted elements within this segment based on match type
      // Keyword matches use .search-keyword-match (word-level)
      // Semantic matches use .search-semantic-segment (full segment text)
      const highlightClass = pos.type === 'keyword'
        ? '.search-keyword-match'
        : '.search-semantic-segment';
      const highlights = segEl.querySelectorAll(highlightClass);

      // Determine pulse class based on match type (yellow for keyword, orange for semantic)
      const pulseClass = pos.type === 'keyword' ? 'search-keyword-pulse' : 'search-semantic-pulse';

      if (highlights.length > 0) {
        // Remove previous pulse from any element
        document.querySelectorAll('.search-keyword-pulse, .search-semantic-pulse').forEach(el => {
          el.classList.remove('search-keyword-pulse', 'search-semantic-pulse');
        });

        // Add pulse to first matched element in this segment
        const targetEl = highlights[0] as HTMLElement;
        targetEl.classList.add(pulseClass);

        // Remove pulse after animation completes (3.5s to match animation)
        setTimeout(() => {
          targetEl.classList.remove(pulseClass);
        }, 3500);

        // Scroll to the highlighted element
        targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } else {
        // Fallback: try any highlight type if specific type not found
        const anyHighlights = segEl.querySelectorAll('.search-keyword-match, .search-semantic-segment, .search-semantic-match');
        if (anyHighlights.length > 0) {
          document.querySelectorAll('.search-keyword-pulse, .search-semantic-pulse').forEach(el => {
            el.classList.remove('search-keyword-pulse', 'search-semantic-pulse');
          });
          const targetEl = anyHighlights[0] as HTMLElement;
          targetEl.classList.add(pulseClass);
          setTimeout(() => {
            targetEl.classList.remove(pulseClass);
          }, 3500);
          targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else {
          // Last fallback: scroll to segment
          segEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }
    });
  }

  async function nextMatch() {
    if (allMatchPositions.length === 0) return;
    const nextIdx = (currentMatchIdx + 1) % allMatchPositions.length;
    await navigateToMatch(nextIdx);
  }

  async function prevMatch() {
    if (allMatchPositions.length === 0) return;
    const prevIdx = currentMatchIdx > 0 ? currentMatchIdx - 1 : allMatchPositions.length - 1;
    await navigateToMatch(prevIdx);
  }

  async function navigateToMatch(targetIdx: number) {
    if (targetIdx < 0 || targetIdx >= allMatchPositions.length) return;

    const targetMatch = allMatchPositions[targetIdx];

    // Try to resolve the segment index from loaded segments using time-based matching
    let resolvedIndex = resolveMatchPositionIndex(targetMatch);

    if (resolvedIndex === -1) {
      // Target segment not yet loaded - load more until we find it
      navigatingToMatch = true;

      while (resolvedIndex === -1 && hasMoreSegments) {
        await loadMoreSegments();
        await tick();
        resolvedIndex = resolveMatchPositionIndex(targetMatch);
      }

      // Wait for DOM to render the element
      if (resolvedIndex !== -1) {
        await waitForSegmentInDOM(resolvedIndex);
      }

      navigatingToMatch = false;
    }

    if (resolvedIndex !== -1) {
      targetMatch.segmentIndex = resolvedIndex;
      targetMatch.resolved = true;
      currentMatchIdx = targetIdx;
      scrollToCurrentMatch();
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (!isOpen) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      if (event.shiftKey) {
        prevMatch();
      } else {
        nextMatch();
      }
    }
  }

  function handleClose() {
    dispatch('close');
  }

  $: totalMatches = allMatchPositions.length;
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="search-transcript-modal-wrapper">
<BaseModal {isOpen} maxWidth="1200px" zIndex={1300} onClose={handleClose}>
      <svelte:fragment slot="header">
        <div class="header-left">
          <h2 class="modal-title">{$t('searchTranscript.title', { fileName })}</h2>
          <div class="match-legend">
            <span class="legend-item keyword-legend">
              <span class="legend-swatch keyword-swatch"></span>
              {$t('searchTranscript.keywordLegend')}
            </span>
            <span class="legend-item semantic-legend">
              <span class="legend-swatch semantic-swatch"></span>
              {$t('searchTranscript.semanticLegend')}
            </span>
          </div>
        </div>
      </svelte:fragment>

      <!-- Navigation Bar -->
      {#if totalMatches > 0}
        <div class="nav-bar">
          <span class="nav-query">"{searchQuery}"</span>
          <div class="nav-controls">
            <span class="nav-count">
              {#if navigatingToMatch}
                <Spinner size="small" />
                {$t('searchTranscript.loadingMatch')}
              {:else}
                {$t('searchTranscript.matchCount', { current: currentMatchIdx + 1, total: totalMatches })}
              {/if}
            </span>
            <button
              class="nav-btn"
              on:click={prevMatch}
              title={$t('searchTranscript.previousMatch')}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="15,18 9,12 15,6"></polyline>
              </svg>
            </button>
            <button
              class="nav-btn"
              on:click={nextMatch}
              title={$t('searchTranscript.nextMatch')}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="9,18 15,12 9,6"></polyline>
              </svg>
            </button>
          </div>
        </div>
      {/if}

      <!-- Content -->
      <div class="modal-content-wrapper">
        <!-- Reading progress bar -->
        {#if groupedSegments.length > 0}
          <div class="reading-progress-bar">
            <div class="reading-progress-fill" style="width: {scrollProgress}%"></div>
          </div>
        {/if}

        <div class="modal-content" bind:this={contentElement} on:scroll={handleContentScroll}>
          {#if loading}
            <div class="state-container">
              <Spinner size="large" />
              <p>{$t('searchTranscript.loading')}</p>
            </div>
          {:else if error}
            <div class="state-container error">
              <p>{$t('searchTranscript.error')}</p>
              <p class="error-detail">{error}</p>
            </div>
          {:else if groupedSegments.length === 0}
            <div class="state-container">
              <p>{$t('searchTranscript.noTranscript')}</p>
            </div>
          {:else}
            <div class="transcript-content">
              {#each groupedSegments as segment}
                <div
                  class="transcript-segment"
                  class:keyword-segment={segment.isKeyword}
                  class:semantic-segment={segment.isSemantic}
                  data-seg-index={segment.rawStartIndex}
                >
                  <div class="segment-header">
                    <div
                      class="segment-speaker"
                      style="background-color: {getSpeakerColorForSegment(segment).bg}; border-color: {getSpeakerColorForSegment(segment).border}; --speaker-light: {getSpeakerColorForSegment(segment).textLight}; --speaker-dark: {getSpeakerColorForSegment(segment).textDark};"
                    >{translateSpeakerLabel(segment.speakerName)}</div>
                    <div class="segment-time">
                      {formatClock(segment.startTime)}-{formatClock(segment.endTime)}
                    </div>
                  </div>
                  <div class="segment-text">{@html sanitizeHighlightHtml(segment.highlightedText)}</div>
                </div>
              {/each}

              <!-- Infinite scroll sentinel -->
              {#if hasMoreSegments}
                <div class="infinite-scroll-sentinel" bind:this={infiniteScrollSentinel}>
                  {#if loadingMoreSegments}
                    <div class="loading-more-indicator">
                      <Spinner size="small" />
                      <span>Loading more...</span>
                    </div>
                  {/if}
                </div>
              {/if}
            </div>
          {/if}
        </div>

        <!-- Segments loaded info -->
        {#if groupedSegments.length > 0}
          <div class="segments-loaded-info">
            <span class="segments-count">{$t('transcript.speakerSegmentsOfTotal', { loaded: groupedSegments.length, total: totalSpeakerSegmentCount || groupedSegments.length })}</span>
            {#if loadingMoreSegments}
              <span class="segments-detail">
                <Spinner size="small" />
              </span>
            {/if}
          </div>
        {/if}
      </div>
</BaseModal>
</div>

<style>
  .header-left {
    flex: 1;
    min-width: 0;
  }

  .modal-title {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 0.375rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .match-legend {
    display: flex;
    gap: 1rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .legend-item {
    display: flex;
    align-items: center;
    gap: 0.375rem;
  }

  .legend-swatch {
    display: inline-block;
    width: 12px;
    height: 12px;
    border-radius: 2px;
  }

  .keyword-swatch {
    background: rgba(250, 204, 21, 0.7);
    border: 1px solid rgba(250, 204, 21, 0.9);
  }

  .semantic-swatch {
    background: rgba(251, 146, 60, 0.6);
    border: 1px solid rgba(251, 146, 60, 0.8);
  }

  /* Override BaseModal body padding for full-bleed layout */
  .search-transcript-modal-wrapper :global(.modal-body) {
    padding: 0 !important;
  }

  /* Navigation Bar */
  .nav-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.625rem 1.5rem;
    border-bottom: 1px solid var(--border-color);
    background-color: var(--bg-secondary);
    flex-shrink: 0;
  }

  .nav-query {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    font-style: italic;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 50%;
  }

  .nav-controls {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .nav-count {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .nav-btn {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 0.25rem;
    cursor: pointer;
    color: var(--text-secondary);
    transition: all 0.15s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 28px;
    min-height: 28px;
  }

  .nav-btn:hover {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    color: var(--text-primary);
  }

  /* Content wrapper for progress bar + scroll area + info bar */
  .modal-content-wrapper {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    position: relative;
  }

  /* Content */
  .modal-content {
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem;
  }

  .state-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 3rem;
    text-align: center;
    color: var(--text-secondary);
  }

  .state-container.error {
    color: var(--error-color, #ef4444);
  }

  .error-detail {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin-top: 0.5rem;
  }

  /* Transcript segments */
  .transcript-content {
    line-height: 1.6;
    font-size: 0.9rem;
  }

  .transcript-segment {
    margin-bottom: 1.25rem;
    display: flex;
    gap: 1rem;
    align-items: flex-start;
    padding: 0.25rem 0;
    border-left: 3px solid transparent;
    padding-left: 0.75rem;
  }

  .transcript-segment:last-child {
    margin-bottom: 0;
  }

  .semantic-segment {
    border-left-color: rgba(251, 146, 60, 0.6);
    border-left-width: 4px;
  }

  :global(.dark) .semantic-segment {
    border-left-color: rgba(251, 146, 60, 0.65);
  }

  .keyword-segment {
    border-left-color: rgba(250, 204, 21, 0.7);
    border-left-width: 4px;
  }

  :global(.dark) .keyword-segment {
    border-left-color: rgba(250, 204, 21, 0.6);
  }

  .segment-header {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    flex-shrink: 0;
    min-width: fit-content;
    align-items: center;
  }

  .segment-speaker {
    font-size: 12px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 12px;
    white-space: nowrap;
    min-width: fit-content;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    border: 1px solid;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--speaker-light);
  }

  :global([data-theme='dark']) .segment-speaker {
    color: var(--speaker-dark);
  }

  .segment-time {
    font-size: 12px;
    font-weight: 600;
    color: var(--primary-color);
    font-family: monospace;
    white-space: nowrap;
  }

  .segment-text {
    font-size: 14px;
    color: var(--text-primary);
    line-height: 1.5;
    flex: 1;
    padding-top: 2px;
  }

  /* Keyword word-level matches - yellow */
  .segment-text :global(.search-keyword-match) {
    background-color: rgba(250, 204, 21, 0.55);
    padding: 0.1em 0.25em;
    border-radius: 3px;
    font-weight: 600;
    border-bottom: 2px solid rgba(250, 204, 21, 0.8);
  }

  :global(.dark) .segment-text :global(.search-keyword-match) {
    background-color: rgba(250, 204, 21, 0.45);
    border-bottom-color: rgba(250, 204, 21, 0.7);
  }

  /* Semantic word-level matches - orange */
  .segment-text :global(.search-semantic-match) {
    background-color: rgba(251, 146, 60, 0.45);
    padding: 0.1em 0.25em;
    border-radius: 3px;
    font-weight: 600;
    border-bottom: 2px solid rgba(251, 146, 60, 0.7);
  }

  :global(.dark) .segment-text :global(.search-semantic-match) {
    background-color: rgba(251, 146, 60, 0.5);
    border-bottom-color: rgba(251, 146, 60, 0.8);
  }

  /* Full semantic segment highlight - when no specific words match query */
  .segment-text :global(.search-semantic-segment) {
    background-color: rgba(251, 146, 60, 0.2);
    border-left: 3px solid rgba(251, 146, 60, 0.6);
    padding: 0.2em 0.4em;
    border-radius: 4px;
    display: inline;
    box-decoration-break: clone;
    -webkit-box-decoration-break: clone;
  }

  :global(.dark) .segment-text :global(.search-semantic-segment) {
    background-color: rgba(251, 146, 60, 0.25);
    border-left-color: rgba(251, 146, 60, 0.7);
  }

  /* Keyword pulse animation - yellow for exact matches */
  :global(.search-keyword-pulse) {
    animation: keyword-pulse 3s ease-in-out;
    border-bottom: 3px solid rgba(250, 204, 21, 0.9) !important;
    background-color: rgba(250, 204, 21, 0.4) !important;
    border-radius: 3px;
  }

  @keyframes keyword-pulse {
    0% {
      box-shadow: 0 0 0 0 rgba(250, 204, 21, 0.6);
    }
    30% {
      box-shadow: 0 0 0 10px rgba(250, 204, 21, 0);
    }
    60% {
      box-shadow: 0 0 0 0 rgba(250, 204, 21, 0.4);
    }
    100% {
      box-shadow: 0 0 0 6px rgba(250, 204, 21, 0);
    }
  }

  /* Semantic pulse animation - orange for similar content matches */
  :global(.search-semantic-pulse) {
    animation: semantic-pulse 3s ease-in-out;
    border-bottom: 3px solid rgba(251, 146, 60, 0.9) !important;
    background-color: rgba(251, 146, 60, 0.35) !important;
    border-radius: 3px;
  }

  @keyframes semantic-pulse {
    0% {
      box-shadow: 0 0 0 0 rgba(251, 146, 60, 0.6);
    }
    30% {
      box-shadow: 0 0 0 10px rgba(251, 146, 60, 0);
    }
    60% {
      box-shadow: 0 0 0 0 rgba(251, 146, 60, 0.4);
    }
    100% {
      box-shadow: 0 0 0 6px rgba(251, 146, 60, 0);
    }
  }

  /* Reading progress bar */
  .reading-progress-bar {
    position: sticky;
    top: 0;
    z-index: 5;
    height: 3px;
    background: var(--border-color, #e5e7eb);
    border-radius: 2px;
    overflow: hidden;
    flex-shrink: 0;
  }

  .reading-progress-fill {
    height: 100%;
    background: var(--primary-color, #4f46e5);
    transition: width 0.15s ease-out;
    border-radius: 2px;
  }

  /* Infinite scroll */
  .infinite-scroll-sentinel {
    min-height: 1px;
    padding: 1rem 0;
  }

  .loading-more-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    padding: 0.75rem;
    color: var(--text-secondary, #6b7280);
    font-size: 0.8125rem;
  }

  .segments-loaded-info {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 8px 16px;
    font-size: 0.75rem;
    color: var(--text-secondary, #9ca3af);
    background: var(--bg-secondary);
    border-top: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .segments-count {
    font-weight: 500;
  }

  .segments-detail {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--text-muted);
  }

  @media (max-width: 768px) {
    .modal-title {
      font-size: 1rem;
    }

    .match-legend {
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .nav-bar {
      padding: 0.5rem 1rem;
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .nav-query {
      max-width: 100%;
      flex: 1 1 100%;
    }

    .nav-btn {
      min-width: 44px;
      min-height: 44px;
    }

    .modal-content {
      padding: 1rem;
    }

    .transcript-segment {
      flex-direction: column;
      gap: 0.5rem;
    }

    .segment-header {
      flex-direction: row;
      gap: 0.5rem;
    }
  }
</style>
