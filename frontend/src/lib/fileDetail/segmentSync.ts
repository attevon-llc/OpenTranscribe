/**
 * Segment state helpers for the file-detail route.
 *
 * `file.transcript_segments` is the SINGLE representation of segment data on this page.
 * `file.grouped_segments` is display grouping that references those segments by uuid and
 * holds no segment data of its own — see `GroupedTranscriptSegment` and
 * `TranscriptDisplay.mapBackendGroup`, which is the only place references are resolved.
 *
 * Every mutation of a segment goes through this module. Before #352 each write path had
 * its own inline loop with its own match rule, which is how a fix applied to one path
 * (`uuid` vs `id` on a synthesized speaker) never reached the others.
 */

import type { GroupedTranscriptSegment } from '$lib/types/media';

/**
 * Largest page `GET /files/{uuid}/segments` will serve. Mirrors `MAX_SEGMENT_PAGE_SIZE`
 * in `backend/app/api/endpoints/files/segments.py`; asking for more is a 422, so callers
 * that need a large range must page in a loop.
 */
export const MAX_SEGMENT_PAGE_SIZE = 2000;

/**
 * The parts of a transcript segment this module reads. Left open-ended because the wire
 * payload carries many more fields (timestamps, redactions, confidence, …) that must be
 * preserved untouched.
 */
export interface SegmentLike {
  uuid?: string;
  speaker_id?: string | null;
  speaker_label?: string | null;
  resolved_speaker_name?: string | null;
  speaker?: { uuid?: string; name?: string; display_name?: string } | null;
  [key: string]: unknown;
}

/** Anything carrying the file-detail transcript payload. */
export interface SegmentBearingFile {
  transcript_segments?: SegmentLike[];
  grouped_segments?: GroupedTranscriptSegment[];
  [key: string]: unknown;
}

export interface SpeakerRename {
  /** Speaker uuid — the primary match key. */
  uuid: string;
  /** New user-facing label. */
  displayName: string;
  /**
   * Original diarization label (`SPEAKER_01`), i.e. the speaker's `name`. Used as a
   * secondary match key, and as `speaker.name` when a segment has no speaker object.
   * Speaker colours hash this value, so it must never be set to a uuid or a display name.
   */
  label?: string;
}

/** One page of `GET /files/{uuid}/segments`. */
export interface SegmentPage {
  transcript_segments?: SegmentLike[];
  grouped_segments?: GroupedTranscriptSegment[];
}

/** Return a replacement segment, or `null` to leave it untouched. */
type SegmentMapper = (segment: SegmentLike) => SegmentLike | null;

/**
 * Apply `mapper` to every segment, returning a new file object.
 *
 * Segments the mapper skips keep their object identity: the segment list is keyed by
 * uuid and several features reach rows via `document.querySelector('[data-segment-id]')`,
 * so needless churn is wasted DOM work.
 *
 * Absence of `transcript_segments` is preserved — the route renders the transcript column
 * behind `{#if file && file.transcript_segments}`, and an empty array is truthy.
 */
export function mapFileSegments<T extends SegmentBearingFile>(file: T, mapper: SegmentMapper): T {
  if (!file) return file;
  const segments = file.transcript_segments;
  if (!Array.isArray(segments)) return { ...file };

  let changed = false;
  const next = segments.map((segment) => {
    const mapped = mapper(segment);
    if (mapped == null || mapped === segment) return segment;
    changed = true;
    return mapped;
  });

  return { ...file, transcript_segments: changed ? next : segments };
}

function matchesRename(segment: SegmentLike, rename: SpeakerRename): boolean {
  if (rename.uuid) {
    if (segment?.speaker_id != null && String(segment.speaker_id) === rename.uuid) return true;
    if (segment?.speaker?.uuid != null && String(segment.speaker.uuid) === rename.uuid) return true;
  }
  if (rename.label) {
    if (segment?.speaker_label === rename.label) return true;
    if (segment?.speaker?.name === rename.label) return true;
  }
  return false;
}

/**
 * Apply speaker display-name changes to every segment of those speakers.
 *
 * Matches on speaker uuid, falling back to the original `SPEAKER_XX` label so this covers
 * both the single-rename path (which knows the uuid) and bulk save (which works from the
 * label). Callers decide *which* speakers to rename — bulk save deliberately skips blank
 * and still-unnamed speakers, and that filter belongs at its call site, not here.
 *
 * Only `resolved_speaker_name` and `speaker.display_name` are written. `speaker_label` and
 * `speaker.name` carry the original diarization id that speaker colours hash, so a rename
 * must never touch them or the segment changes colour.
 */
export function renameSpeakersInFile<T extends SegmentBearingFile>(
  file: T,
  renames: SpeakerRename[]
): T {
  if (!file || !renames?.length) return file;

  return mapFileSegments(file, (segment) => {
    const rename = renames.find((candidate) => matchesRename(segment, candidate));
    if (!rename) return null;

    const speaker = segment.speaker
      ? { ...segment.speaker, display_name: rename.displayName }
      : {
          // `uuid`, not `id` — SegmentSpeakerDropdown and transcriptStore both read
          // `segment.speaker.uuid`, so an `id` key leaves the speaker unmatchable.
          uuid: rename.uuid,
          // Never the uuid: this feeds the colour hash.
          name: segment.speaker_label || rename.label || rename.uuid,
          display_name: rename.displayName,
        };

    return { ...segment, resolved_speaker_name: rename.displayName, speaker };
  });
}

/** Shallow-merge `patch` into one segment, matched by uuid. */
export function patchSegmentInFile<T extends SegmentBearingFile>(
  file: T,
  segmentUuid: string,
  patch: object
): T {
  if (!file || !segmentUuid) return file;
  const target = String(segmentUuid);

  return mapFileSegments(file, (segment) =>
    segment?.uuid != null && String(segment.uuid) === target ? { ...segment, ...patch } : null
  );
}

/**
 * Drop uuids already claimed by an earlier group, and drop groups left empty.
 *
 * A uuid appearing in two groups would render the same segment twice under the same
 * `{#each ... (segment.uuid)}` key, which Svelte rejects at runtime — that takes down the
 * whole transcript list, so this is a crash guard, not tidiness.
 */
function dedupeGroups(groups: GroupedTranscriptSegment[]): GroupedTranscriptSegment[] {
  const seen = new Set<string>();
  const out: GroupedTranscriptSegment[] = [];

  for (const group of groups) {
    const original = group.segment_uuids || [];
    const uuids = original.map(String).filter((uuid) => !seen.has(uuid));
    if (!uuids.length) continue;
    uuids.forEach((uuid) => seen.add(uuid));

    out.push(
      uuids.length === original.length
        ? group
        : {
            ...group,
            segment_uuids: uuids,
            is_overlap_group: group.is_overlap_group && uuids.length > 1,
          }
    );
  }

  return out;
}

/**
 * Stitch an overlap run that straddles a pagination boundary.
 *
 * The backend groups each page independently, so a run spanning the boundary arrives as a
 * trailing group on page N and a leading group on page N+1 sharing one `overlap_group_id`.
 * Left unmerged they render as two groups with the same key.
 */
function mergeGroupPages(
  existing: GroupedTranscriptSegment[],
  incoming: GroupedTranscriptSegment[]
): GroupedTranscriptSegment[] {
  if (!incoming.length) return existing;
  if (!existing.length) return [...incoming];

  const last = existing[existing.length - 1];
  const first = incoming[0];
  const sharedOverlap =
    last.overlap_group_id != null &&
    first.overlap_group_id != null &&
    String(last.overlap_group_id) === String(first.overlap_group_id);

  if (!sharedOverlap) return [...existing, ...incoming];

  const segmentUuids = [...(last.segment_uuids || []), ...(first.segment_uuids || [])];
  const merged: GroupedTranscriptSegment = {
    ...last,
    is_overlap_group: segmentUuids.length > 1,
    start_time: Math.min(last.start_time, first.start_time),
    end_time: Math.max(last.end_time, first.end_time),
    // The run starts on the earlier page, so its index is the one that stays.
    start_segment_index: last.start_segment_index,
    segment_uuids: segmentUuids,
  };

  return [...existing.slice(0, -1), merged, ...incoming.slice(1)];
}

/**
 * Append a page of segments and their grouping, returning a new file object.
 *
 * Both representations must advance together: the transcript renders from the grouping, so
 * appending segments alone loaded rows that were never displayed (#352).
 */
export function appendSegmentPage<T extends SegmentBearingFile>(file: T, page: SegmentPage): T {
  if (!file) return file;

  const existingSegments = file.transcript_segments || [];
  const seen = new Set(existingSegments.map((segment) => String(segment?.uuid)));
  const added = (page?.transcript_segments || []).filter(
    (segment) => segment?.uuid != null && !seen.has(String(segment.uuid))
  );

  return {
    ...file,
    transcript_segments: [...existingSegments, ...added],
    grouped_segments: dedupeGroups(
      mergeGroupPages(file.grouped_segments || [], page?.grouped_segments || [])
    ),
  };
}
