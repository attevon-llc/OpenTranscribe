/**
 * Utility to map FFmpeg metadata format to backend's metadata schema
 *
 * Maps FFmpeg/FFprobe output to match backend/app/tasks/transcription/metadata_extractor.py
 */

import type { VideoMetadata } from '../types/audioExtraction';

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
  const formatTags = format?.tags || {};
  const videoTags = videoStream?.tags || {};

  const metadata: VideoMetadata = {};

  // Basic file info
  metadata.FileName = file.name;
  metadata.FileSize = file.size;
  metadata.MIMEType = file.type;
  metadata.FileType = format?.format_name;
  metadata.FileTypeExtension = file.name.split('.').pop();

  // Video specs
  if (videoStream) {
    metadata.VideoFormat = videoStream.codec_name;
    metadata.VideoWidth = videoStream.width;
    metadata.VideoHeight = videoStream.height;
    metadata.VideoCodec = videoStream.codec_name;

    const frameRate = parseFrameRate(videoStream.r_frame_rate || videoStream.avg_frame_rate);
    if (frameRate) {
      metadata.FrameRate = frameRate;
      metadata.VideoFrameRate = frameRate;
    }

    // Calculate aspect ratio if we have dimensions
    if (videoStream.width && videoStream.height) {
      const gcd = (a: number, b: number): number => (b === 0 ? a : gcd(b, a % b));
      const divisor = gcd(videoStream.width, videoStream.height);
      metadata.AspectRatio = `${videoStream.width / divisor}:${videoStream.height / divisor}`;
    }
  }

  // Audio specs
  if (audioStream) {
    metadata.AudioFormat = audioStream.codec_name;
    metadata.AudioChannels = audioStream.channels;
    metadata.AudioSampleRate = audioStream.sample_rate
      ? parseInt(audioStream.sample_rate)
      : undefined;
    metadata.AudioBitsPerSample = audioStream.bits_per_sample;
  }

  // Duration (prefer format duration, fallback to stream duration)
  if (format?.duration) {
    metadata.Duration = parseFloat(format.duration);
  } else if (videoStream?.duration) {
    metadata.Duration = parseFloat(videoStream.duration);
  }

  // Creation and modification dates
  // Check multiple possible tag names (different containers use different names)
  const creationDateFields = [
    'creation_time',
    'date',
    'creation_date',
    'content_create_date',
    'datetime_original',
    'year',
  ];

  for (const field of creationDateFields) {
    const dateValue = formatTags[field] || videoTags[field];
    if (dateValue) {
      const parsed = parseMediaDate(dateValue);
      if (parsed && !metadata.CreateDate) {
        metadata.CreateDate = parsed;
        break;
      }
    }
  }

  // Fallback to File.lastModified if no creation date found
  if (!metadata.CreateDate) {
    metadata.CreateDate = new Date(file.lastModified).toISOString();
  }

  // Modification date
  if (formatTags.modification_time) {
    metadata.ModifyDate = parseMediaDate(formatTags.modification_time);
  }

  // Device information
  metadata.DeviceManufacturer = formatTags.make || videoTags.make;
  metadata.DeviceModel = formatTags.model || videoTags.model;

  // GPS information (if present)
  // ISO 6709 (e.g. "+40.6894-074.0447+002.000/") concatenates signed lat/lon with no
  // delimiter between them, so a delimiter split can't separate the two coordinates.
  // The trailing coordinate's sign is required (it's what marks where longitude starts);
  // the leading one is optional to still accept a plain "40.7128,-74.0060" tag.
  const gpsMatch = formatTags.location?.match(/^([+-]?\d+\.?\d*)[,\s]*([+-]\d+\.?\d*)/);
  metadata.GPSLatitude = gpsMatch?.[1];
  metadata.GPSLongitude = gpsMatch?.[2];

  // Software used
  metadata.Software = formatTags.encoder || formatTags.software;

  // Content information
  metadata.Title = formatTags.title || videoTags.title;
  metadata.Artist = formatTags.artist || videoTags.artist;
  metadata.Author = formatTags.author || videoTags.author || formatTags.artist;
  metadata.Comment = formatTags.comment || videoTags.comment;
  metadata.Description = formatTags.description || videoTags.description;
  metadata.LongDescription = formatTags.synopsis || formatTags.summary;

  // Add any other tags that might be useful
  for (const [key, value] of Object.entries(formatTags)) {
    if (
      !metadata[key] &&
      ['creator', 'copyright', 'language', 'genre', 'album', 'track'].some((term) =>
        key.toLowerCase().includes(term)
      )
    ) {
      metadata[key] = value;
    }
  }

  return metadata;
}

/**
 * Estimate compressed audio size based on video duration
 *
 * @param duration - Video duration in seconds
 * @param bitrate - Target audio bitrate in kbps (default: 32)
 * @returns Estimated audio file size in bytes
 */
export function estimateAudioSize(duration: number, bitrate: number = 32): number {
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

/**
 * Format file size for display
 *
 * @param bytes - File size in bytes
 * @returns Formatted string (e.g., "45.3 MB")
 */
export function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 B';

  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const k = 1024;
  const rawIndex = Math.floor(Math.log(bytes) / Math.log(k));
  const i = Math.min(Math.max(rawIndex, 0), units.length - 1);
  const size = bytes / Math.pow(k, i);

  return `${size.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}
