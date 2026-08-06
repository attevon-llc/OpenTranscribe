/**
 * Types for client-side audio extraction from video files
 *
 * Handles metadata preservation, progress tracking, and upload integration
 */

/**
 * Video metadata extracted from original file using FFmpeg
 * Maps to backend's metadata_extractor.py schema
 */
export interface VideoMetadata {
  // Basic file info
  FileName?: string;
  FileSize?: number;
  MIMEType?: string;
  FileType?: string;
  FileTypeExtension?: string;

  // Video specs
  VideoFormat?: string;
  Duration?: number;
  FrameRate?: number;
  FrameCount?: number;
  VideoFrameRate?: number;
  VideoWidth?: number;
  VideoHeight?: number;
  AspectRatio?: string;
  VideoCodec?: string;

  // Audio specs
  AudioFormat?: string;
  AudioChannels?: number;
  AudioSampleRate?: number;
  AudioBitsPerSample?: number;

  // Creation info
  CreateDate?: string;
  ModifyDate?: string;
  DateTimeOriginal?: string;

  // Device info
  DeviceManufacturer?: string;
  DeviceModel?: string;

  // GPS info
  GPSLatitude?: string;
  GPSLongitude?: string;

  // Software used
  Software?: string;

  // Content information
  Title?: string;
  Artist?: string;
  Author?: string;
  Comment?: string;
  Description?: string;
  LongDescription?: string;
  HandlerDescription?: string;
  Encoder?: string;

  // Raw metadata from FFmpeg (including audio_codec)
  RawMetadata?: Record<string, any>;

  // Any additional metadata fields
  [key: string]: string | number | Record<string, any> | undefined;
}

/**
 * Complete metadata for an extracted audio file
 */
export interface ExtractedAudioMetadata {
  // Original video file information
  originalFileName: string;
  originalFileSize: number;
  originalFileType: string;
  originalLastModified: number; // Unix timestamp from File.lastModified
  originalFingerprint: string; // imohash of the original video, for duplicate detection

  // Extracted audio file information
  extractedAudioSize: number;
  extractedFileName: string;
  extractedFileType: string; // Usually 'audio/opus'

  // Compression and extraction info
  compressionRatio: number; // Percentage (0-100)
  extractionDate: string; // ISO 8601
  extractionDuration: number; // Milliseconds taken to extract

  // Video metadata extracted via FFmpeg
  videoMetadata: VideoMetadata;
}

/**
 * Result of audio extraction process
 */
export interface ExtractedAudio {
  blob: Blob;
  filename: string;
  metadata: ExtractedAudioMetadata;
}

/**
 * Progress event during extraction
 */
export interface ExtractionProgress {
  stage: 'initializing' | 'metadata' | 'extracting' | 'finalizing';
  percentage: number; // 0-100
  message: string;
  bytesProcessed?: number;
  totalBytes?: number;
  estimatedTimeRemaining?: number; // Milliseconds
}

/**
 * Extraction error with context
 */
export interface ExtractionError {
  code: string;
  message: string;
  stage: ExtractionProgress['stage'];
  originalError?: Error;
}

/**
 * Audio extraction service configuration
 */
export interface AudioExtractionConfig {
  // Output format settings
  outputFormat: 'opus' | 'mp3';
  bitrate: number; // kbps
  sampleRate: number; // Hz
  channels: number; // 1 = mono, 2 = stereo

  // Processing settings
  maxFileSize: number; // Maximum video file size to process (bytes)
  workerUrl?: string; // Optional custom worker URL

  // Progress callback
  onProgress?: (progress: ExtractionProgress) => void;
}

/**
 * Default configuration for audio extraction
 */
export const DEFAULT_EXTRACTION_CONFIG: AudioExtractionConfig = {
  outputFormat: 'mp3',
  bitrate: 64, // 64kbps for good quality MP3 (Whisper works great with this)
  sampleRate: 16000, // 16kHz (Whisper's native rate)
  channels: 1, // Mono
  maxFileSize: 2 * 1024 * 1024 * 1024, // 2GB (FFmpeg.wasm limit)
};

/**
 * User preferences for audio extraction
 */
export interface AudioExtractionPreferences {
  autoExtractLargeVideos: boolean; // Auto-extract videos >100MB
  showExtractionModal: boolean; // Show modal before extraction
  rememberChoice: boolean; // Remember user's choice
  notificationPreference: 'detailed' | 'minimal'; // Notification verbosity
}

/**
 * Notification data for extraction progress
 */
export interface ExtractionNotificationData {
  extractionId: string;
  fileName: string;
  originalSize: number;
  estimatedAudioSize: number;
  compressionRatio: number;
  stage: ExtractionProgress['stage'];
  percentage: number;
}
