/**
 * Transcript paging on the file-detail page: the infinite-scroll "load more", the
 * jump-to-segment load (speaker editor), and the post-merge refetch (#603).
 *
 * Every page goes through `appendSegmentPage`, so both segment representations advance
 * together (#352). Each request honours the reader's "show original" flag. The page owns the
 * state and exposes it through {@link SegmentPagerContext}, as `notificationHandler` does.
 */
import axiosInstance from '$lib/axios';
import { appendSegmentPage, MAX_SEGMENT_PAGE_SIZE, type SegmentBearingFile } from './segmentSync';
import { scrollToAndHighlight, type Schedule } from './segmentDom';

/** Query param that asks the server for the unmasked transcript (owner "show original"). */
export function redactParams(showOriginal: boolean): { redact?: false } {
  return showOriginal ? { redact: false } : {};
}

export interface SegmentPagerContext {
  getFileId: () => string;
  getFile: () => SegmentBearingFile | null;
  /** Must perform the page's real `file = ...` assignment. */
  setFile: (file: SegmentBearingFile) => void;
  getShowOriginal: () => boolean;
  getSegmentLimit: () => number;
  getTotalSegments: () => number;
  setTotalSegments: (total: number) => void;
  isLoadingMore: () => boolean;
  setLoadingMore: (loading: boolean) => void;
  hasMoreSegments: () => boolean;
  /** Re-feed the transcript store with the current segments and speakers. */
  syncTranscriptStore: () => void;
  schedule: Schedule;
  /** Called with the i18n key of a failure the reader must be told about. */
  notifyError: (key: string) => void;
}

function loadedCount(file: SegmentBearingFile | null): number {
  return file?.transcript_segments?.length || 0;
}

export function createSegmentPager(ctx: SegmentPagerContext) {
  function segmentsUrl(): string {
    return `/files/${ctx.getFileId()}/segments`;
  }

  /** Load the next page for large transcripts (infinite scroll, modal "load more"). */
  async function loadMore(): Promise<void> {
    if (!ctx.getFileId() || ctx.isLoadingMore() || !ctx.hasMoreSegments()) return;

    try {
      ctx.setLoadingMore(true);
      const response = await axiosInstance.get(segmentsUrl(), {
        params: {
          segment_limit: ctx.getSegmentLimit(),
          segment_offset: loadedCount(ctx.getFile()),
          ...redactParams(ctx.getShowOriginal()),
        },
      });

      if (response.data && response.data.transcript_segments) {
        // Both representations advance together — the transcript renders from the
        // grouping, so appending segments alone loads rows that never display (#352).
        ctx.setFile(appendSegmentPage(ctx.getFile() as SegmentBearingFile, response.data));
        ctx.setTotalSegments(response.data.total_segments || ctx.getTotalSegments());
        ctx.syncTranscriptStore();
      }
    } catch (error) {
      console.error('Error loading more segments:', error);
      ctx.notifyError('fileDetail.failedToLoadMoreSegments');
    } finally {
      ctx.setLoadingMore(false);
    }
  }

  /**
   * Load segments up to a target index for jump-to-timestamp navigation, then scroll to
   * and flash the target segment.
   */
  async function loadUpTo(targetIndex: number, segmentUuid: string): Promise<void> {
    if (!ctx.getFileId() || ctx.isLoadingMore()) return;

    if (targetIndex < loadedCount(ctx.getFile())) {
      // Already loaded - just scroll and highlight
      scrollToAndHighlight(segmentUuid, ctx.schedule);
      return;
    }

    try {
      ctx.setLoadingMore(true);
      const buffer = 50;

      // Page in a loop: the endpoint caps a single page, so a jump to the end of a long
      // transcript can't be satisfied by one request. The guard bounds the loop if the
      // server stops making progress.
      for (let page = 0; page < 50; page++) {
        const loaded = loadedCount(ctx.getFile());
        if (loaded > targetIndex) break;

        const response = await axiosInstance.get(segmentsUrl(), {
          params: {
            segment_limit: Math.min(targetIndex - loaded + buffer + 1, MAX_SEGMENT_PAGE_SIZE),
            segment_offset: loaded,
            ...redactParams(ctx.getShowOriginal()),
          },
        });

        const fetched = response.data?.transcript_segments;
        if (!Array.isArray(fetched) || fetched.length === 0) break;

        ctx.setFile(appendSegmentPage(ctx.getFile() as SegmentBearingFile, response.data));
        ctx.setTotalSegments(response.data.total_segments || ctx.getTotalSegments());

        if (loadedCount(ctx.getFile()) >= ctx.getTotalSegments()) break;
      }

      ctx.syncTranscriptStore();

      // Wait for DOM update then scroll to target segment
      ctx.schedule(() => scrollToAndHighlight(segmentUuid, ctx.schedule), 300);
    } catch (error) {
      console.error('Error loading segments up to target:', error);
      ctx.notifyError('fileDetail.failedToLoadMoreSegments');
    } finally {
      ctx.setLoadingMore(false);
    }
  }

  /**
   * Re-fetch segments after a speaker merge (issue #603).
   *
   * A merge deletes the SOURCE speaker's Postgres row and re-labels its segments under
   * the TARGET speaker. `speaker_processing_complete` for a merge carries only the target
   * uuid, so `applySpeakerRename` — which patches segments that still hold a KNOWN uuid —
   * has no old-to-new mapping and can't find the segments still holding the now-dead
   * source uuid client-side. Merges are rare enough that refetching is the honest fix:
   * reset to empty and replay the same paging path `loadMore`/`loadUpTo` use
   * (`appendSegmentPage`, which skips already-seen uuids — starting from empty is what
   * turns that into a full replace), preserving however many segments were already
   * loaded rather than force-loading the rest of a not-yet-fully-loaded transcript.
   */
  async function refetchAfterMerge(): Promise<void> {
    const file = ctx.getFile();
    if (!ctx.getFileId() || !file) return;

    const targetCount = loadedCount(file);
    ctx.setFile({ ...file, transcript_segments: [], grouped_segments: [] });
    if (targetCount === 0) return;

    const limit = ctx.getSegmentLimit();
    try {
      for (let offset = 0; offset < targetCount; offset += limit) {
        const response = await axiosInstance.get(segmentsUrl(), {
          params: {
            segment_limit: Math.min(limit, targetCount - offset),
            segment_offset: offset,
            ...redactParams(ctx.getShowOriginal()),
          },
        });
        if (!response.data?.transcript_segments) break;
        ctx.setFile(appendSegmentPage(ctx.getFile() as SegmentBearingFile, response.data));
      }

      ctx.syncTranscriptStore();
    } catch (error) {
      console.error('Error refetching segments after speaker merge:', error);
    }
  }

  return { loadMore, loadUpTo, refetchAfterMerge };
}
