/**
 * Utility to map FFmpeg metadata format to backend's metadata schema
 *
 * Maps FFmpeg/FFprobe output to match backend/app/tasks/transcription/metadata_extractor.py
 */

import { DEFAULT_EXTRACTION_CONFIG } from '$lib/types/audioExtraction';
import type { VideoMetadata } from '$lib/types/audioExtraction';

/**
 * Parse FFmpeg metadata from JSON output
 *
 * FFmpeg provides metadata in streams and format objects
 */
interface FFmpegStream {
  codec_name?: string;
  codec_type?: string;
  width?: number;
  height?: number;
  r_frame_rate?: string;
  avg_frame_rate?: string;
  duration?: string;
  bit_rate?: string;
  channels?: number;
  sample_rate?: string;
  bits_per_sample?: number;
  tags?: Record<string, string>;
}

interface FFmpegFormat {
  filename?: string;
  format_name?: string;
  format_long_name?: string;
  duration?: string;
  size?: string;
  bit_rate?: string;
  tags?: Record<string, string>;
}

interface FFmpegProbeData {
  streams?: FFmpegStream[];
  format?: FFmpegFormat;
}

/**
 * Parse frame rate string (e.g., "30000/1001" or "30") to number
 */
function parseFrameRate(frameRateStr: string | undefined): number | undefined {
  if (!frameRateStr) return undefined;

  try {
    if (frameRateStr.includes('/')) {
      const [num, den] = frameRateStr.split('/').map(Number);
      if (den === 0) return undefined;
      return num / den;
    }
    return parseFloat(frameRateStr);
  } catch {
    return undefined;
  }
}

/**
 * Parse date string from various FFmpeg metadata formats
 */
function parseMediaDate(dateStr: string | undefined): string | undefined {
  if (!dateStr) return undefined;

  // Invalid/placeholder dates to reject
  const invalidDates = [
    '0000:00:00 00:00:00',
    '0000-00-00 00:00:00',
    '1970:01:01 00:00:00',
    '1970-01-01T00:00:00',
  ];

  if (invalidDates.includes(dateStr.trim())) {
    return undefined;
  }

  // Return as-is, backend will parse it
  return dateStr.trim();
}

type Tags = Record<string, string | undefined>;
type FFmpegStreamData = NonNullable<FFmpegProbeData['streams']>[number];

function videoSpecs(stream: FFmpegStreamData): VideoMetadata {
  const specs: VideoMetadata = {
    VideoFormat: stream.codec_name,
    VideoWidth: stream.width,
    VideoHeight: stream.height,
    VideoCodec: stream.codec_name,
  };
  const frameRate = parseFrameRate(stream.r_frame_rate || stream.avg_frame_rate);
  if (frameRate) {
    specs.FrameRate = frameRate;
    specs.VideoFrameRate = frameRate;
  }
  // Calculate aspect ratio if we have dimensions
  if (stream.width && stream.height) {
    const gcd = (a: number, b: number): number => (b === 0 ? a : gcd(b, a % b));
    const divisor = gcd(stream.width, stream.height);
    specs.AspectRatio = `${stream.width / divisor}:${stream.height / divisor}`;
  }
  return specs;
}

function audioSpecs(stream: FFmpegStreamData): VideoMetadata {
  return {
    AudioFormat: stream.codec_name,
    AudioChannels: stream.channels,
    AudioSampleRate: stream.sample_rate ? parseInt(stream.sample_rate) : undefined,
    AudioBitsPerSample: stream.bits_per_sample,
  };
}

// Prefer the container's duration, fall back to the video stream's.
function durationSeconds(
  format: FFmpegProbeData['format'],
  videoStream: FFmpegStreamData | undefined
): number | undefined {
  if (format?.duration) return parseFloat(format.duration);
  if (videoStream?.duration) return parseFloat(videoStream.duration);
  return undefined;
}

// Different containers use different tag names for the creation date.
const CREATION_DATE_FIELDS = [
  'creation_time',
  'date',
  'creation_date',
  'content_create_date',
  'datetime_original',
  'year',
];

function creationDate(formatTags: Tags, videoTags: Tags): string | undefined {
  for (const field of CREATION_DATE_FIELDS) {
    const parsed = parseMediaDate(formatTags[field] || videoTags[field]);
    if (parsed) return parsed;
  }
  return undefined;
}

// ISO 6709 (e.g. "+40.6894-074.0447+002.000/") concatenates signed lat/lon with no
// delimiter between them, so a delimiter split can't separate the two coordinates.
// The trailing coordinate's sign is required (it's what marks where longitude starts);
// the leading one is optional to still accept a plain "40.7128,-74.0060" tag.
function gpsFromTags(formatTags: Tags): Pick<VideoMetadata, 'GPSLatitude' | 'GPSLongitude'> {
  const match = formatTags.location?.match(/^([+-]?\d+\.?\d*)[,\s]*([+-]\d+\.?\d*)/);
  return { GPSLatitude: match?.[1], GPSLongitude: match?.[2] };
}

function contentTags(formatTags: Tags, videoTags: Tags): VideoMetadata {
  return {
    DeviceManufacturer: formatTags.make || videoTags.make,
    DeviceModel: formatTags.model || videoTags.model,
    Software: formatTags.encoder || formatTags.software,
    Title: formatTags.title || videoTags.title,
    Artist: formatTags.artist || videoTags.artist,
    Author: formatTags.author || videoTags.author || formatTags.artist,
    Comment: formatTags.comment || videoTags.comment,
    Description: formatTags.description || videoTags.description,
    LongDescription: formatTags.synopsis || formatTags.summary,
  };
}

function datesOf(file: File, formatTags: Tags, videoTags: Tags): VideoMetadata {
  const dates: VideoMetadata = {
    // Fall back to File.lastModified if no creation date is found
    CreateDate: creationDate(formatTags, videoTags) ?? new Date(file.lastModified).toISOString(),
  };
  if (formatTags.modification_time) {
    dates.ModifyDate = parseMediaDate(formatTags.modification_time);
  }
  return dates;
}

const EXTRA_TAG_TERMS = ['creator', 'copyright', 'language', 'genre', 'album', 'track'];

// Other tags that might be useful, kept when no mapped field already holds them.
function addExtraTags(metadata: VideoMetadata, formatTags: Tags): void {
  for (const [key, value] of Object.entries(formatTags)) {
    if (!metadata[key] && EXTRA_TAG_TERMS.some((term) => key.toLowerCase().includes(term))) {
      metadata[key] = value;
    }
  }
}

/**
 * Map FFmpeg probe data to backend's VideoMetadata schema
 *
 * @param probeData - FFmpeg probe output (JSON parsed)
 * @param file - Original File object for additional metadata
 * @returns Mapped metadata matching backend schema
 */
export function mapFFmpegMetadata(probeData: FFmpegProbeData, file: File): VideoMetadata {
  const videoStream = probeData.streams?.find((s) => s.codec_type === 'video');
  const audioStream = probeData.streams?.find((s) => s.codec_type === 'audio');
  const format = probeData.format;
  const formatTags: Tags = format?.tags || {};
  const videoTags: Tags = videoStream?.tags || {};

  const metadata: VideoMetadata = {
    FileName: file.name,
    FileSize: file.size,
    MIMEType: file.type,
    FileType: format?.format_name,
    FileTypeExtension: file.name.split('.').pop(),
    ...(videoStream ? videoSpecs(videoStream) : {}),
    ...(audioStream ? audioSpecs(audioStream) : {}),
  };

  const duration = durationSeconds(format, videoStream);
  if (duration !== undefined) metadata.Duration = duration;

  Object.assign(metadata, datesOf(file, formatTags, videoTags));
  Object.assign(metadata, gpsFromTags(formatTags), contentTags(formatTags, videoTags));
  addExtraTags(metadata, formatTags);
  return metadata;
}

/**
 * Rough heuristic for a video's duration before its real metadata is available,
 * based on a typical size-to-duration ratio. Shared by the upload wizard and the
 * bulk extraction modal so both size estimates are derived the same way.
 *
 * @param fileSizeBytes - Video file size in bytes
 * @returns Estimated duration in seconds
 */
export function estimateDurationFromFileSize(fileSizeBytes: number): number {
  return (fileSizeBytes / (1024 * 1024)) * 60;
}

/**
 * Estimate compressed audio size based on video duration
 *
 * @param duration - Video duration in seconds
 * @param bitrate - Target audio bitrate in kbps. Defaults to
 *   `DEFAULT_EXTRACTION_CONFIG.bitrate` — the single source of truth for the
 *   real extraction bitrate (G3: two call sites previously hardcoded their own
 *   copy of this number, one of them wrong, and this default disagreed with
 *   both).
 * @returns Estimated audio file size in bytes
 */
export function estimateAudioSize(
  duration: number,
  bitrate: number = DEFAULT_EXTRACTION_CONFIG.bitrate
): number {
  // bitrate is in kbps, convert to bytes per second
  const bytesPerSecond = (bitrate * 1000) / 8;

  // Add 10% overhead for container format
  const overhead = 1.1;

  return Math.round(duration * bytesPerSecond * overhead);
}

/**
 * Calculate compression ratio
 *
 * @param originalSize - Original video file size in bytes
 * @param compressedSize - Compressed audio file size in bytes
 * @returns Compression ratio as percentage (0-100)
 */
export function calculateCompressionRatio(originalSize: number, compressedSize: number): number {
  if (originalSize === 0) return 0;
  const ratio = ((originalSize - compressedSize) / originalSize) * 100;
  return Math.round(Math.max(0, Math.min(100, ratio)));
}
