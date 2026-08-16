/**
 * Mediabunny-based audio extraction — EVALUATION MODULE, not wired into the app.
 *
 * Built for issue #473's side-by-side comparison against the self-compiled FFmpeg.wasm core
 * (`audioExtractionService.ts`). Mediabunny (MPL-2.0, npm `mediabunny`) is a pure-TypeScript
 * media toolkit using the browser's native WebCodecs API for decode/encode — no compiled WASM
 * binary, so no FFmpeg license question at all.
 *
 * Deliberately NOT the shipped path. Two gaps, both verified during evaluation (see
 * `backend/tests/e2e/test_audio_extraction_comparison.py` and `.legal/02-licensing-ip/
 * MASTER-LICENSE-INVENTORY.md` §8):
 *   1. No AVI, FLV, or WMV/ASF container support (Mediabunny only ships ISOBMFF/mov, Matroska/
 *      WebM, Ogg, MP3, WAV, ADTS, FLAC, MPEG-TS, HLS demuxers/muxers) — the FFmpeg core handles
 *      all of those.
 *   2. Depends on the WebCodecs API (~93.6% global browser support; Firefox <130 and Safari
 *      <16.4 have none at all) — the WASM path works anywhere WebAssembly does.
 * Kept here, tested, and importable for anyone who wants to re-run the comparison or revisit
 * the decision — not deleted, since re-deriving the API usage from docs each time is wasted
 * work.
 */

import {
  Input,
  Output,
  Conversion,
  ALL_FORMATS,
  BlobSource,
  BufferTarget,
  Mp4OutputFormat,
  Mp3OutputFormat,
  OggOutputFormat,
  FlacOutputFormat,
  WavOutputFormat,
  type OutputFormat,
  type AudioCodec,
} from 'mediabunny';
import type {
  ExtractedAudio,
  ExtractedAudioMetadata,
  VideoMetadata,
} from '../types/audioExtraction';
import { calculateCompressionRatio } from '../utils/metadataMapper';
import { fingerprintFile } from '$lib/services/fileFingerprint';

/** Mirrors `audioExtractionService.ts`'s `getAudioExtension`/`getAudioMimeType` codec table. */
const CODEC_OUTPUT: Record<
  string,
  { format: () => OutputFormat; extension: string; mimeType: string }
> = {
  aac: { format: () => new Mp4OutputFormat(), extension: 'm4a', mimeType: 'audio/mp4' },
  mp3: { format: () => new Mp3OutputFormat(), extension: 'mp3', mimeType: 'audio/mpeg' },
  opus: { format: () => new OggOutputFormat(), extension: 'opus', mimeType: 'audio/opus' },
  vorbis: { format: () => new OggOutputFormat(), extension: 'ogg', mimeType: 'audio/ogg' },
  flac: { format: () => new FlacOutputFormat(), extension: 'flac', mimeType: 'audio/flac' },
  'pcm-s16': { format: () => new WavOutputFormat(), extension: 'wav', mimeType: 'audio/wav' },
  'pcm-s24': { format: () => new WavOutputFormat(), extension: 'wav', mimeType: 'audio/wav' },
  'pcm-s32': { format: () => new WavOutputFormat(), extension: 'wav', mimeType: 'audio/wav' },
};
const DEFAULT_OUTPUT = CODEC_OUTPUT.aac;

export class MediabunnyExtractionService {
  public isSupported(): boolean {
    return (
      typeof globalThis.VideoDecoder !== 'undefined' &&
      typeof globalThis.AudioDecoder !== 'undefined'
    );
  }

  public async extractMetadata(file: File): Promise<VideoMetadata> {
    const input = new Input({ formats: ALL_FORMATS, source: new BlobSource(file) });
    try {
      const format = await input.getFormat();
      const duration = await input.computeDuration();
      const audioTrack = await input.getPrimaryAudioTrack();
      const codec = audioTrack ? await audioTrack.getCodec() : null;

      return {
        FileName: file.name,
        FileSize: file.size,
        MIMEType: file.type,
        FileType: file.type.split('/')[1],
        FileTypeExtension: file.name.split('.').pop(),
        CreateDate: new Date(file.lastModified).toISOString(),
        ModifyDate: new Date(file.lastModified).toISOString(),
        Duration: duration,
        RawMetadata: {
          audio_codec: codec ?? undefined,
          container_format: format.constructor.name,
        },
      };
    } finally {
      input.dispose();
    }
  }

  public async extractAudio(file: File): Promise<ExtractedAudio> {
    const startTime = Date.now();
    const originalFingerprint = await fingerprintFile(file);

    const input = new Input({ formats: ALL_FORMATS, source: new BlobSource(file) });
    try {
      const audioTrack = await input.getPrimaryAudioTrack();
      const codec: AudioCodec | null = audioTrack ? await audioTrack.getCodec() : null;
      const outputSpec = (codec && CODEC_OUTPUT[codec]) || DEFAULT_OUTPUT;

      const output = new Output({ format: outputSpec.format(), target: new BufferTarget() });
      const conversion = await Conversion.init({ input, output, video: { discard: true } });

      if (!conversion.isValid) {
        const reasons = conversion.discardedTracks.map((t) => t.reason).join(', ');
        throw new Error(`Mediabunny conversion invalid: ${reasons || 'no audio track'}`);
      }

      await conversion.execute();

      const buffer = (output.target as BufferTarget).buffer;
      if (!buffer) {
        throw new Error('Mediabunny conversion produced no output buffer');
      }

      const audioBlob = new Blob([buffer], { type: outputSpec.mimeType });
      const metadata = await this.extractMetadata(file);
      const extractionDuration = Date.now() - startTime;
      const compressionRatio = calculateCompressionRatio(file.size, audioBlob.size);

      const extractedMetadata: ExtractedAudioMetadata = {
        originalFileName: file.name,
        originalFileSize: file.size,
        originalFileType: file.type,
        originalLastModified: file.lastModified,
        originalFingerprint,
        extractedAudioSize: audioBlob.size,
        extractedFileName: file.name.replace(/\.[^.]+$/, `.${outputSpec.extension}`),
        extractedFileType: audioBlob.type,
        compressionRatio,
        extractionDate: new Date().toISOString(),
        extractionDuration,
        videoMetadata: metadata,
      };

      return {
        blob: audioBlob,
        filename: extractedMetadata.extractedFileName,
        metadata: extractedMetadata,
      };
    } finally {
      input.dispose();
    }
  }
}

export const mediabunnyExtractionService = new MediabunnyExtractionService();
