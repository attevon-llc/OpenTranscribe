/**
 * Audio Extraction Service
 *
 * Client-side video-to-audio extraction using FFmpeg.wasm
 * Handles metadata preservation, progress tracking, and error handling
 */

import { FFmpeg } from '@ffmpeg/ffmpeg';
import { toBlobURL, fetchFile } from '@ffmpeg/util';
import type {
  ExtractedAudio,
  ExtractedAudioMetadata,
  ExtractionProgress,
  ExtractionError,
  AudioExtractionConfig,
  VideoMetadata,
} from '../types/audioExtraction';
import { DEFAULT_EXTRACTION_CONFIG } from '../types/audioExtraction';
import {
  mapFFmpegMetadata,
  estimateAudioSize,
  calculateCompressionRatio,
} from '../utils/metadataMapper';
import { websocketStore } from '../../stores/websocket';
import { get } from 'svelte/store';
// Hashing runs in a Web Worker so the UI stays responsive. Audio extraction runs on
// VIDEO files — the largest inputs the app accepts (15 GB limit) — and the previous
// main-thread `file.arrayBuffer()` + `crypto.subtle.digest` buffered the whole file
// into memory and blocked the event loop for its duration (issue #302).
import { hashFileSHA256 } from '$lib/services/sha256Hasher';
import { t } from '../../stores/locale';
import { generateId } from '$lib/utils/ids';

/**
 * Audio Extraction Service Class
 *
 * Manages FFmpeg.wasm instance and provides audio extraction functionality
 */
class AudioExtractionService {
  private ffmpeg: FFmpeg | null = null;
  private isLoaded: boolean = false;
  private isLoading: boolean = false;
  private loadPromise: Promise<void> | null = null;

  // Event handlers
  private progressHandlers: Set<(progress: ExtractionProgress) => void> = new Set();
  private errorHandlers: Set<(error: ExtractionError) => void> = new Set();

  // Current extraction tracking for notifications
  private currentExtractionId: string | null = null;
  private currentFileName: string | null = null;

  // Queue management for sequential processing
  private extractionQueue: Array<{
    file: File;
    config?: Partial<AudioExtractionConfig>;
    resolve: (value: ExtractedAudio) => void;
    reject: (error: any) => void;
  }> = [];
  private isProcessing: boolean = false;

  /**
   * Check if browser supports FFmpeg.wasm
   */
  public isSupported(): boolean {
    // FFmpeg.wasm works in single-threaded mode without SharedArrayBuffer
    // Just check if we have the basic APIs we need
    if (typeof WebAssembly === 'undefined') {
      return false;
    }

    // Check for Worker support (needed for FFmpeg.wasm)
    if (typeof Worker === 'undefined') {
      return false;
    }

    return true;
  }

  /**
   * Load FFmpeg.wasm (lazy loading)
   */
  private async load(): Promise<void> {
    // If already loaded, return immediately
    if (this.isLoaded) {
      return;
    }

    // If currently loading, wait for existing load promise
    if (this.isLoading && this.loadPromise) {
      return this.loadPromise;
    }

    // Start loading
    this.isLoading = true;
    this.loadPromise = this._doLoad();

    try {
      await this.loadPromise;
      this.isLoaded = true;
    } finally {
      this.isLoading = false;
    }
  }

  /**
   * Internal load implementation
   */
  private async _doLoad(): Promise<void> {
    try {
      this.ffmpeg = new FFmpeg();

      // Set up progress logging - only log errors to avoid console spam
      this.ffmpeg.on('log', ({ message }) => {
        // Only log errors, not info messages or warnings
        if (message.toLowerCase().includes('error')) {
          console.error('[FFmpeg Error]', message);
        }
      });

      // Set up progress events
      this.ffmpeg.on('progress', ({ progress }) => {
        // FFmpeg progress is 0-1, convert to 0-100
        const percentage = Math.round(progress * 100);
        this.emitProgress({
          stage: 'extracting',
          percentage,
          message: get(t)('extraction.progressExtractingPercent', { percent: percentage }),
        });
      });

      // Load FFmpeg core from local files (frontend/static/ffmpeg/, populated by the
      // download-ffmpeg.js prebuild step and copied verbatim into dist/ffmpeg/).
      const baseURL = '/ffmpeg';
      await this.ffmpeg.load({
        coreURL: await toBlobURL(`${baseURL}/ffmpeg-core.js`, 'text/javascript'),
        wasmURL: await toBlobURL(`${baseURL}/ffmpeg-core.wasm`, 'application/wasm'),
      });

      console.log('[AudioExtractionService] FFmpeg loaded successfully');
    } catch (error) {
      console.error('[AudioExtractionService] Failed to load FFmpeg:', error);
      throw new Error(
        get(t)('extraction.errorFfmpegLoadFailed', {
          error: error instanceof Error ? error.message : String(error),
        })
      );
    }
  }

  /**
   * Map audio codec to file extension
   */
  private getAudioExtension(codec: string): string {
    const codecMap: Record<string, string> = {
      aac: 'm4a',
      mp3: 'mp3',
      opus: 'opus',
      vorbis: 'ogg',
      flac: 'flac',
      pcm_s16le: 'wav',
      pcm_s24le: 'wav',
      pcm_s32le: 'wav',
    };
    return codecMap[codec.toLowerCase()] || 'm4a'; // Default to m4a for unknown codecs
  }

  /**
   * Get MIME type from audio codec
   */
  private getAudioMimeType(codec: string): string {
    const mimeMap: Record<string, string> = {
      aac: 'audio/mp4',
      mp3: 'audio/mpeg',
      opus: 'audio/opus',
      vorbis: 'audio/ogg',
      flac: 'audio/flac',
      pcm_s16le: 'audio/wav',
      pcm_s24le: 'audio/wav',
      pcm_s32le: 'audio/wav',
    };
    return mimeMap[codec.toLowerCase()] || 'audio/mp4';
  }

  /**
   * Extract metadata from video file using FFmpeg
   */
  public async extractMetadata(file: File): Promise<VideoMetadata> {
    if (!this.isSupported()) {
      throw new Error(get(t)('extraction.errorFfmpegUnsupported'));
    }

    await this.load();
    if (!this.ffmpeg) {
      throw new Error(get(t)('extraction.errorFfmpegNotInitialized'));
    }

    try {
      this.emitProgress({
        stage: 'metadata',
        percentage: 5,
        message: get(t)('extraction.progressReadingMetadata'),
      });

      // Capture FFmpeg metadata output
      const metadataFromFFmpeg: Record<string, any> = {};
      let captureMetadata = false;

      const logHandler = ({ message }: { message: string }) => {
        // Start capturing when we see "Metadata:" line
        if (message.trim() === 'Metadata:') {
          captureMetadata = true;
          return;
        }

        // Stop capturing when we see Duration or Stream
        if (message.includes('Duration:') || message.includes('Stream #')) {
          captureMetadata = false;
        }

        // Capture metadata fields
        if (captureMetadata && message.includes(':')) {
          const match = message.match(/^\s+(\w+)\s*:\s*(.+)$/);
          if (match) {
            const [, key, value] = match;
            metadataFromFFmpeg[key] = value.trim();
          }
        }

        // Capture duration
        if (message.includes('Duration:')) {
          const durationMatch = message.match(/Duration: (\d+):(\d+):(\d+\.\d+)/);
          if (durationMatch) {
            const [, hours, minutes, seconds] = durationMatch;
            metadataFromFFmpeg.duration = `${hours}:${minutes}:${seconds}`;
          }
        }

        // Capture audio codec from stream info
        if (message.includes('Stream #') && message.includes('Audio:')) {
          const codecMatch = message.match(/Audio:\s*(\w+)/);
          if (codecMatch) {
            metadataFromFFmpeg.audio_codec = codecMatch[1];
          }
        }
      };

      // Temporarily add log handler
      this.ffmpeg.on('log', logHandler);

      // Write file to FFmpeg filesystem temporarily to trigger metadata reading
      const tempFileName = `metadata_${Date.now()}${this.getFileExtension(file.name)}`;
      await this.ffmpeg.writeFile(tempFileName, await fetchFile(file));

      // Run ffmpeg to read metadata (very fast, doesn't process the whole file)
      // Use -c copy to avoid decoding, just read container metadata
      await this.ffmpeg.exec(['-i', tempFileName, '-c', 'copy', '-f', 'null', '-']);

      // Clean up
      await this.ffmpeg.deleteFile(tempFileName);
      this.ffmpeg.off('log', logHandler);

      // Build metadata object
      const metadata: VideoMetadata = {
        FileName: file.name,
        FileSize: file.size,
        MIMEType: file.type,
        FileType: file.type.split('/')[1],
        FileTypeExtension: file.name.split('.').pop(),
        CreateDate: new Date(file.lastModified).toISOString(),
        ModifyDate: new Date(file.lastModified).toISOString(),
        // Add FFmpeg extracted metadata
        Title: metadataFromFFmpeg.title,
        Artist: metadataFromFFmpeg.artist,
        Description: metadataFromFFmpeg.description,
        Comment: metadataFromFFmpeg.comment,
        Duration: metadataFromFFmpeg.duration,
        Encoder: metadataFromFFmpeg.encoder,
        // Store all raw metadata for backend
        RawMetadata: metadataFromFFmpeg,
      };

      this.emitProgress({
        stage: 'metadata',
        percentage: 10,
        message: get(t)('extraction.progressMetadataDone'),
      });

      return metadata;
    } catch (error) {
      console.error('[AudioExtractionService] Metadata extraction failed:', error);
      // Fall back to basic metadata if extraction fails
      return {
        FileName: file.name,
        FileSize: file.size,
        MIMEType: file.type,
        FileType: file.type.split('/')[1],
        FileTypeExtension: file.name.split('.').pop(),
        CreateDate: new Date(file.lastModified).toISOString(),
        ModifyDate: new Date(file.lastModified).toISOString(),
      };
    }
  }

  /**
   * Extract audio from video file (queued for sequential processing)
   */
  public async extractAudio(
    file: File,
    config: Partial<AudioExtractionConfig> = {}
  ): Promise<ExtractedAudio> {
    // Add to queue and return a promise
    return new Promise<ExtractedAudio>((resolve, reject) => {
      this.extractionQueue.push({ file, config, resolve, reject });
      this.processQueue();
    });
  }

  /**
   * Process extraction queue sequentially
   */
  private async processQueue(): Promise<void> {
    // If already processing or queue is empty, return
    if (this.isProcessing || this.extractionQueue.length === 0) {
      return;
    }

    this.isProcessing = true;

    while (this.extractionQueue.length > 0) {
      const item = this.extractionQueue.shift();
      if (!item) break;

      try {
        const result = await this._extractAudioInternal(item.file, item.config);
        item.resolve(result);
      } catch (error) {
        item.reject(error);
      }
    }

    this.isProcessing = false;
  }

  /**
   * Internal method to extract audio from video file
   */
  private async _extractAudioInternal(
    file: File,
    config: Partial<AudioExtractionConfig> = {}
  ): Promise<ExtractedAudio> {
    if (!this.isSupported()) {
      throw this.createError(
        'UNSUPPORTED_BROWSER',
        get(t)('extraction.errorBrowserUnsupported'),
        'initializing'
      );
    }

    // Merge config with defaults
    const finalConfig: AudioExtractionConfig = {
      ...DEFAULT_EXTRACTION_CONFIG,
      ...config,
    };

    // Check file size
    if (file.size > finalConfig.maxFileSize) {
      throw this.createError(
        'FILE_TOO_LARGE',
        get(t)('extraction.errorFileTooLarge', {
          size: Math.round(file.size / (1024 * 1024 * 1024)),
          max: Math.round(finalConfig.maxFileSize / (1024 * 1024 * 1024)),
        }),
        'initializing'
      );
    }

    const startTime = Date.now();

    // Set up tracking for notifications - use unique ID for each extraction
    const extractionId = generateId('extraction');
    const fileName = file.name;

    try {
      // Load FFmpeg if not already loaded
      this.emitProgress(
        {
          stage: 'initializing',
          percentage: 0,
          message: get(t)('extraction.progressInitializing'),
        },
        extractionId,
        fileName
      );

      await this.load();
      if (!this.ffmpeg) {
        throw new Error(get(t)('extraction.errorFfmpegNotInitialized'));
      }

      // Calculate original file hash for duplicate detection
      this.emitProgress(
        {
          stage: 'metadata',
          percentage: 2,
          message: get(t)('upload.calculatingHash'),
        },
        extractionId,
        fileName
      );

      const originalFileHash = await hashFileSHA256(file);

      // Extract metadata first
      const metadata = await this.extractMetadata(file);

      // Detect audio codec to determine output extension
      const audioCodec = metadata.RawMetadata?.audio_codec || 'aac';
      const outputExtension = this.getAudioExtension(audioCodec);

      // Write input file to FFmpeg virtual filesystem
      this.emitProgress(
        {
          stage: 'extracting',
          percentage: 15,
          message: get(t)('extraction.progressLoadingVideo'),
        },
        extractionId,
        fileName
      );

      // Use unique filenames based on extractionId to support concurrent extractions
      const uniqueId = extractionId.split('-')[1]; // Use timestamp portion for shorter name
      const inputFileName = `input_${uniqueId}${this.getFileExtension(file.name)}`;
      const outputFileName = `output_${uniqueId}.${outputExtension}`;

      await this.ffmpeg.writeFile(inputFileName, await fetchFile(file));

      // Build FFmpeg command for audio extraction
      this.emitProgress(
        {
          stage: 'extracting',
          percentage: 20,
          message: get(t)('extraction.progressExtracting'),
        },
        extractionId,
        fileName
      );

      // Use stream copy for fast extraction without re-encoding
      const ffmpegArgs = [
        '-i',
        inputFileName,
        '-vn', // No video
        '-c:a',
        'copy', // Copy audio stream without re-encoding
        outputFileName,
      ];

      await this.ffmpeg.exec(ffmpegArgs);

      // Read output file
      this.emitProgress(
        {
          stage: 'finalizing',
          percentage: 95,
          message: get(t)('extraction.progressFinalizing'),
        },
        extractionId,
        fileName
      );

      const audioData = await this.ffmpeg.readFile(outputFileName);
      // Ensure proper BlobPart compatibility - readFile returns Uint8Array or string
      // Use slice() to create a new ArrayBuffer that is not SharedArrayBuffer
      const blobData =
        typeof audioData === 'string' ? new TextEncoder().encode(audioData) : audioData.slice();
      const audioBlob = new Blob([blobData], {
        type: this.getAudioMimeType(audioCodec),
      });

      // Clean up FFmpeg filesystem
      await this.ffmpeg.deleteFile(inputFileName);
      await this.ffmpeg.deleteFile(outputFileName);

      // Build complete metadata
      const extractionDuration = Date.now() - startTime;
      const compressionRatio = calculateCompressionRatio(file.size, audioBlob.size);

      const extractedMetadata: ExtractedAudioMetadata = {
        originalFileName: file.name,
        originalFileSize: file.size,
        originalFileType: file.type,
        originalLastModified: file.lastModified,
        originalFileHash: originalFileHash,
        extractedAudioSize: audioBlob.size,
        extractedFileName: file.name.replace(/\.[^.]+$/, `.${finalConfig.outputFormat}`),
        extractedFileType: audioBlob.type,
        compressionRatio,
        extractionDate: new Date().toISOString(),
        extractionDuration,
        videoMetadata: metadata,
      };

      this.emitProgress(
        {
          stage: 'finalizing',
          percentage: 100,
          message: get(t)('extraction.progressComplete'),
        },
        extractionId,
        fileName
      );

      return {
        blob: audioBlob,
        filename: extractedMetadata.extractedFileName,
        metadata: extractedMetadata,
      };
    } catch (error) {
      console.error('[AudioExtractionService] Extraction failed:', error);

      const errorMessage = error instanceof Error ? error.message : String(error);
      throw this.createError(
        'EXTRACTION_FAILED',
        get(t)('extraction.errorFailed', { error: errorMessage }),
        'extracting',
        error as Error
      );
    }
  }

  /**
   * Register progress handler
   */
  public onProgress(handler: (progress: ExtractionProgress) => void): () => void {
    this.progressHandlers.add(handler);
    return () => this.progressHandlers.delete(handler);
  }

  /**
   * Register error handler
   */
  public onError(handler: (error: ExtractionError) => void): () => void {
    this.errorHandlers.add(handler);
    return () => this.errorHandlers.delete(handler);
  }

  /**
   * Emit progress event and send to notification panel
   */
  private emitProgress(
    progress: ExtractionProgress,
    extractionId?: string,
    fileName?: string
  ): void {
    // Emit to local handlers
    this.progressHandlers.forEach((handler) => handler(progress));

    // Send to notification panel if we have extraction tracking info
    if (extractionId && fileName) {
      websocketStore.addNotification({
        type: 'audio_extraction_status',
        title: get(t)('notifications.audioExtraction'),
        message: fileName,
        progressId: extractionId,
        currentStep: progress.message,
        progress: {
          current: progress.percentage,
          total: 100,
          percentage: progress.percentage,
        },
        status: progress.percentage === 100 ? 'completed' : 'processing',
        dismissible: progress.percentage === 100,
        silent: false,
      });
    }
  }

  /**
   * Emit error event
   */
  private emitError(error: ExtractionError): void {
    this.errorHandlers.forEach((handler) => handler(error));
  }

  /**
   * Create typed error
   */
  private createError(
    code: string,
    message: string,
    stage: ExtractionProgress['stage'],
    originalError?: Error
  ): ExtractionError {
    return {
      code,
      message,
      stage,
      originalError,
    };
  }

  /**
   * Get file extension from filename
   */
  private getFileExtension(filename: string): string {
    const ext = filename.split('.').pop();
    return ext ? `.${ext}` : '.mp4';
  }

  /**
   * Cleanup FFmpeg instance
   */
  public async cleanup(): Promise<void> {
    if (this.ffmpeg) {
      // FFmpeg.wasm doesn't have explicit cleanup, but we can reset state
      this.ffmpeg = null;
      this.isLoaded = false;
      this.isLoading = false;
      this.loadPromise = null;
    }
  }
}

// Export singleton instance
export const audioExtractionService = new AudioExtractionService();

// Export class for testing
export { AudioExtractionService };
