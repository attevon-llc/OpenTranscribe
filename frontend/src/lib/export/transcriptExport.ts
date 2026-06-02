/**
 * Client-side transcript export serialization.
 *
 * Approved client-side exception to the thin-frontend rule: a purely-presentational
 * transform over already-downloaded transcript data (TXT/JSON/CSV/SRT/VTT). Extracted
 * verbatim from the file-detail page so the logic is typed and testable.
 *
 * This module is intentionally Svelte-free: it imports no stores and resolves no i18n.
 * The caller resolves every user-visible string (via `$t(...)`) and passes them in
 * through `opts.translations`. Time formatting is delegated to the single shared home
 * in `$lib/utils/formatting`.
 */

import { formatDuration, formatSrtTimestamp, formatVttTimestamp } from '$lib/utils/formatting';

export type ExportFormat = 'txt' | 'json' | 'csv' | 'srt' | 'vtt';

/**
 * A transcript segment as consumed by the export serializer. Tolerates both the
 * `start_time`/`end_time` and legacy `start`/`end` field names, and resolves a
 * speaker from `speaker_label` or a nested `speaker` object.
 */
export interface ExportSegment {
  start_time?: number;
  end_time?: number;
  start?: number;
  end?: number;
  text: string;
  speaker_label?: string;
  speaker?: { name?: string; display_name?: string };
}

/** A speaker entry used to map raw names to display names. */
export interface ExportSpeaker {
  name: string;
  display_name?: string;
}

/** A comment entry merged positionally into the export. */
export interface ExportComment {
  timestamp: number;
  text: string;
  user?: { full_name?: string; username?: string; email?: string };
}

/**
 * The i18n strings the serialization switch needs. The caller resolves each via
 * `$t(...)` and passes them in so this module stays store-free.
 */
export interface ExportStrings {
  /** `$t('fileDetail.speakerDefault')` — fallback speaker label. */
  speakerDefault: string;
  /** `$t('fileDetail.userComment')` — "USER COMMENT" prefix in CSV/SRT/VTT comment rows. */
  userComment: string;
  /** `$t('fileDetail.commentType')` — "COMMENT" value for the CSV Comment Type column. */
  commentType: string;
  /** `$t('fileDetail.csvHeaderDefault')` — CSV header without the comment column. */
  csvHeaderDefault: string;
  /** `$t('fileDetail.csvHeaderWithComments')` — CSV header with the comment column. */
  csvHeaderWithComments: string;
}

export interface ExportOptions {
  includeComments: boolean;
  txtOptions?: { includeTimestamps: boolean; includeSpeakers: boolean };
  /**
   * Base filename (extension stripped) — used to name the downloaded file by the caller.
   * NOTE: the JSON body's own `filename`/`duration` fields come from `jsonMeta`, which
   * carries the original (with-extension) filename and duration to stay byte-identical.
   */
  filename: string;
  /** File metadata embedded only in the JSON export body. */
  jsonMeta?: { filename: string; duration: number | null | undefined };
  translations: ExportStrings;
}

/**
 * Merges transcript segments and comments in chronological order.
 * @param segments - Array of formatted transcript segment strings
 * @param commentLines - Array of formatted comment strings
 * @param transcriptData - Raw transcript data with timestamps
 * @param comments - Raw comments data with timestamps
 * @returns Combined array of segments and comments in order
 */
export function mergeCommentsWithTranscript(
  segments: string[],
  commentLines: string[],
  transcriptData: ExportSegment[],
  comments: ExportComment[]
): string[] {
  // Create arrays of timestamps for sorting
  const segmentTimes = transcriptData.map((seg) => seg.start_time || seg.start || 0);
  const commentTimes = comments.map((comment) => comment.timestamp);

  // Create merged array of segment and comment entries
  let merged = [];
  let si = 0,
    ci = 0;

  // Merge two sorted arrays (segments and comments) by timestamp
  while (si < segments.length && ci < commentLines.length) {
    if (segmentTimes[si] <= commentTimes[ci]) {
      merged.push(segments[si]);
      si++;
    } else {
      merged.push(commentLines[ci]);
      ci++;
    }
  }

  // Add any remaining segments
  while (si < segments.length) {
    merged.push(segments[si]);
    si++;
  }

  // Add any remaining comments
  while (ci < commentLines.length) {
    merged.push(commentLines[ci]);
    ci++;
  }

  return merged;
}

/**
 * Merges two arrays based on their corresponding timestamp arrays.
 * @param arr1 - First array
 * @param arr2 - Second array
 * @param times1 - Timestamps for first array
 * @param times2 - Timestamps for second array
 * @returns Merged array in chronological order
 */
export function mergeSortedArrays<T>(
  arr1: T[],
  arr2: T[],
  times1: number[],
  times2: number[]
): T[] {
  let merged = [];
  let i = 0,
    j = 0;

  while (i < arr1.length && j < arr2.length) {
    if (times1[i] <= times2[j]) {
      merged.push(arr1[i]);
      i++;
    } else {
      merged.push(arr2[j]);
      j++;
    }
  }

  while (i < arr1.length) {
    merged.push(arr1[i]);
    i++;
  }

  while (j < arr2.length) {
    merged.push(arr2[j]);
    j++;
  }

  return merged;
}

/**
 * Serializes transcript data (and optionally comments) into the requested export
 * format. Pure string-building — the caller is responsible for fetching/sorting
 * comments, resolving speaker display names into `speakerList`, resolving i18n
 * strings, and triggering the actual download.
 *
 * @param format - Output format.
 * @param transcriptData - Transcript segments. Sorted by start_time before use.
 * @param speakerList - Speakers, used to map raw names to display names.
 * @param fileComments - Comments (already user-enriched + timestamp-sorted by caller).
 * @param opts - Export options including resolved translations.
 * @returns The serialized file content (byte-identical to the legacy inline switch).
 */
export function buildExportContent(
  format: ExportFormat,
  transcriptData: ExportSegment[],
  speakerList: ExportSpeaker[],
  fileComments: ExportComment[],
  opts: ExportOptions
): string {
  const { includeComments, txtOptions, jsonMeta, translations } = opts;
  const t = translations;

  // Sort transcript data by start_time to ensure proper ordering
  transcriptData = [...transcriptData].sort((a, b) => (a.start_time ?? 0) - (b.start_time ?? 0));

  // Create speaker display name mapping
  const speakerMapping = new Map<string, string>();
  speakerList.forEach((speaker) => {
    speakerMapping.set(speaker.name, speaker.display_name || speaker.name);
  });

  // Helper function to get speaker display name
  const getSpeakerDisplayName = (segment: ExportSegment) => {
    const speakerName = segment.speaker_label || segment.speaker?.name || t.speakerDefault;
    return speakerMapping.get(speakerName) || segment.speaker?.display_name || speakerName;
  };

  // Client-side export with updated speaker names
  let content = '';

  switch (format) {
    case 'txt': {
      // Group consecutive segments by the same speaker
      const speakerGroups: Array<{
        speaker: string;
        startTime: number;
        endTime: number;
        texts: string[];
      }> = [];
      let currentGroup: (typeof speakerGroups)[0] | null = null;

      for (const seg of transcriptData) {
        const speaker = getSpeakerDisplayName(seg);
        const startTime = seg.start_time || seg.start || 0;
        const endTime = seg.end_time || seg.end || 0;

        if (currentGroup && currentGroup.speaker === speaker) {
          currentGroup.endTime = endTime;
          currentGroup.texts.push(seg.text);
        } else {
          if (currentGroup) speakerGroups.push(currentGroup);
          currentGroup = { speaker, startTime, endTime, texts: [seg.text] };
        }
      }
      if (currentGroup) speakerGroups.push(currentGroup);

      // Format each group as a single block
      let segments = speakerGroups.map((group) => {
        const parts: string[] = [];
        if (txtOptions?.includeTimestamps !== false) {
          parts.push(`[${formatDuration(group.startTime)} --> ${formatDuration(group.endTime)}]`);
        }
        if (txtOptions?.includeSpeakers !== false) {
          parts.push(`${group.speaker}:`);
        }
        const header = parts.join(' ');
        const text = group.texts.join(' ');
        return header ? `${header}\n${text}` : text;
      });

      // Add comments if requested (comments always retain their timestamps since they are positional)
      if (includeComments && fileComments.length > 0) {
        const commentLines = fileComments.map((comment) => {
          const userName =
            comment.user?.full_name || comment.user?.username || comment.user?.email || 'Anonymous';
          return `[${formatDuration(comment.timestamp)}] USER COMMENT: ${userName}: ${
            comment.text
          }`;
        });
        segments = mergeCommentsWithTranscript(
          segments,
          commentLines,
          transcriptData,
          fileComments
        );
      }

      content = segments.join('\n\n');
      break;
    }
    case 'json': {
      const jsonData: {
        filename: string | undefined;
        duration: number | null | undefined;
        segments: Array<{ start_time: number; end_time: number; speaker: string; text: string }>;
        comments?: Array<{ timestamp: number; user: string; text: string }>;
      } = {
        filename: jsonMeta?.filename,
        duration: jsonMeta?.duration,
        segments: transcriptData.map((seg) => ({
          start_time: seg.start_time || seg.start || 0,
          end_time: seg.end_time || seg.end || 0,
          speaker: getSpeakerDisplayName(seg),
          text: seg.text,
        })),
      };

      // Add comments to JSON if requested
      if (includeComments && fileComments.length > 0) {
        jsonData.comments = fileComments.map((comment) => ({
          timestamp: comment.timestamp,
          user:
            comment.user?.full_name || comment.user?.username || comment.user?.email || 'Anonymous',
          text: comment.text,
        }));
      }

      content = JSON.stringify(jsonData, null, 2);
      break;
    }
    case 'csv': {
      let csvHeader = t.csvHeaderDefault;
      let csvRows = transcriptData.map((seg) => {
        const start = seg.start_time || seg.start || 0;
        const end = seg.end_time || seg.end || 0;
        const speaker = getSpeakerDisplayName(seg);
        // Escape CSV fields properly
        const escapedText = `"${seg.text.replace(/"/g, '""')}"`;
        return `${start},${end},"${speaker}",${escapedText}`;
      });

      // Add comments to CSV if requested
      if (includeComments && fileComments.length > 0) {
        // Add comment column to header if comments are included
        csvHeader = t.csvHeaderWithComments;

        // Add comments as separate rows
        const commentRows = fileComments.map((comment) => {
          const timestamp = comment.timestamp;
          const userName =
            comment.user?.full_name || comment.user?.username || comment.user?.email || 'Anonymous';
          const escapedText = `"${comment.text.replace(/"/g, '""')}"`;
          // Add comment rows with user info in the Speaker column and 'COMMENT' in Comment Type
          return `${timestamp},${timestamp},"${t.userComment}: ${userName}",${escapedText},"${t.commentType}"`;
        });

        // Combine segment rows (with empty Comment Type) with comment rows
        csvRows = csvRows.map((row: string) => row + ',""');
        csvRows = mergeSortedArrays(
          csvRows,
          commentRows,
          transcriptData.map((seg) => seg.start_time || seg.start || 0),
          fileComments.map((comment) => comment.timestamp)
        );
      }

      content = csvHeader + '\n' + csvRows.join('\n');
      break;
    }
    case 'srt': {
      let srtItems: Array<{
        index: number;
        startTime: number;
        endTime: number;
        formattedStart: string;
        formattedEnd: string;
        text: string;
        isComment: boolean;
      }> = [];
      let counter = 1;

      // Add transcript segments
      transcriptData.forEach((seg) => {
        const startTime = formatSrtTimestamp(seg.start_time || seg.start || 0);
        const endTime = formatSrtTimestamp(seg.end_time || seg.end || 0);
        const speaker = getSpeakerDisplayName(seg);
        const text = `${speaker}: ${seg.text}`;
        srtItems.push({
          index: counter++,
          startTime: seg.start_time || seg.start || 0,
          endTime: seg.end_time || seg.end || 0,
          formattedStart: startTime,
          formattedEnd: endTime,
          text: text,
          isComment: false,
        });
      });

      // Add comments if requested
      if (includeComments && fileComments.length > 0) {
        fileComments.forEach((comment) => {
          const timestamp = comment.timestamp;
          const formattedTime = formatSrtTimestamp(timestamp);
          const userName =
            comment.user?.full_name || comment.user?.username || comment.user?.email || 'Anonymous';
          const text = `${t.userComment}: ${userName}: ${comment.text}`;

          srtItems.push({
            index: counter++,
            startTime: timestamp,
            endTime: timestamp + 2, // Show comment for 2 seconds
            formattedStart: formattedTime,
            formattedEnd: formatSrtTimestamp(timestamp + 2),
            text: text,
            isComment: true,
          });
        });

        // Sort by start time
        srtItems.sort((a, b) => a.startTime - b.startTime);

        // Reassign indices after sorting
        srtItems.forEach((item, idx) => {
          item.index = idx + 1;
        });
      }

      // Generate SRT content
      content = srtItems
        .map(
          (item) => `${item.index}\n${item.formattedStart} --> ${item.formattedEnd}\n${item.text}\n`
        )
        .join('\n');
      break;
    }
    case 'vtt': {
      let vttItems: Array<{
        startTime: number;
        endTime: number;
        formattedStart: string;
        formattedEnd: string;
        text: string;
        isComment: boolean;
      }> = [];

      // Add transcript segments
      transcriptData.forEach((seg) => {
        const startTime = formatVttTimestamp(seg.start_time || seg.start || 0);
        const endTime = formatVttTimestamp(seg.end_time || seg.end || 0);
        const speaker = getSpeakerDisplayName(seg);
        const text = `${speaker}: ${seg.text}`;
        vttItems.push({
          startTime: seg.start_time || seg.start || 0,
          endTime: seg.end_time || seg.end || 0,
          formattedStart: startTime,
          formattedEnd: endTime,
          text: text,
          isComment: false,
        });
      });

      // Add comments if requested
      if (includeComments && fileComments.length > 0) {
        fileComments.forEach((comment) => {
          const timestamp = comment.timestamp;
          const formattedTime = formatVttTimestamp(timestamp);
          const userName =
            comment.user?.full_name || comment.user?.username || comment.user?.email || 'Anonymous';
          const text = `${t.userComment}: ${userName}: ${comment.text}`;

          vttItems.push({
            startTime: timestamp,
            endTime: timestamp + 2, // Show comment for 2 seconds
            formattedStart: formattedTime,
            formattedEnd: formatVttTimestamp(timestamp + 2),
            text: text,
            isComment: true,
          });
        });

        // Sort by start time
        vttItems.sort((a, b) => a.startTime - b.startTime);
      }

      // Generate VTT content
      content =
        'WEBVTT\n\n' +
        vttItems
          .map((item) => `${item.formattedStart} --> ${item.formattedEnd}\n${item.text}\n`)
          .join('\n');
      break;
    }
    default:
      content = transcriptData.map((seg) => seg.text).join(' ');
  }

  return content;
}
