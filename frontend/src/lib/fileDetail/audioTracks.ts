/**
 * Pure rules for the audio tracks of a multi-track file (#1263): which tracks a file offers,
 * how one is described, and the request field that picks it. The server owns the data
 * (`audio_streams` from ffprobe at ingest) and validates the choice; nothing here probes media.
 */
import type { Translate } from '$lib/i18n';

/** One audio track, mirroring the server's `AudioStreamInfo`. */
export interface AudioStream {
  /** The N of ffmpeg's `-map 0:a:N`: the ordinal among audio tracks, from 0. */
  index: number;
  codec?: string | null;
  channels?: number | null;
  /** The container's language tag as written, usually ISO 639-2 (`por`); null when unset. */
  language?: string | null;
  title?: string | null;
  default?: boolean;
}

/** Tracks worth choosing between: a file with one track (or none probed) offers no choice. */
export function selectableTracks(streams: AudioStream[] | null | undefined): AudioStream[] {
  return streams && streams.length > 1 ? streams : [];
}

/** The track the pipeline decodes: the chosen one, else the first. */
export function transcribedTrack(
  streams: AudioStream[] | null | undefined,
  chosen: number | null | undefined
): AudioStream | null {
  const wanted = chosen ?? 0;
  return streams?.find((s) => s.index === wanted) ?? null;
}

/** A language code as a name in the UI language; the code itself when the runtime does not know it. */
export function languageName(code: string | null | undefined, uiLocale: string): string | null {
  if (!code) return null;
  try {
    const name = new Intl.DisplayNames([uiLocale], { type: 'language' }).of(code);
    if (name && name.toLowerCase() !== code.toLowerCase()) return name;
  } catch {
    // An unknown or malformed code falls through to showing the code.
  }
  return code.toUpperCase();
}

function channelLabel(channels: number | null | undefined, t: Translate): string | null {
  if (!channels) return null;
  if (channels === 1) return t('audioTracks.mono');
  if (channels === 2) return t('audioTracks.stereo');
  return t('audioTracks.channels', { n: channels });
}

/** "Portuguese · Stereo · AAC · Director's dub" — whatever the container gave us, in that order. */
export function describeTrack(stream: AudioStream, t: Translate, uiLocale: string): string {
  const parts = [
    languageName(stream.language, uiLocale) ?? t('audioTracks.untagged'),
    channelLabel(stream.channels, t),
    stream.codec ? stream.codec.toUpperCase() : null,
    stream.title ?? null,
  ];
  return parts.filter((p): p is string => !!p).join(' · ');
}

/** The `<option>` text: "Track 2: Portuguese · Stereo · AAC (default)". */
export function trackOptionLabel(stream: AudioStream, t: Translate, uiLocale: string): string {
  const base = t('audioTracks.optionLabel', {
    number: stream.index + 1,
    description: describeTrack(stream, t, uiLocale),
  });
  return stream.default ? `${base} (${t('audioTracks.default')})` : base;
}

/** The `audio_stream_index` field for a reprocess body; absent unless the user picked a track. */
export function audioTrackBodyField(
  choice: number | null,
  streams: AudioStream[] | null | undefined
): { audio_stream_index?: number } {
  return choice !== null && selectableTracks(streams).some((s) => s.index === choice)
    ? { audio_stream_index: choice }
    : {};
}
