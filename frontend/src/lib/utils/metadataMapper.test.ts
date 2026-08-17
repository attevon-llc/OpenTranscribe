/**
 * `mapFFmpegMetadata` translates FFprobe's JSON into the backend's VideoMetadata
 * schema. The module's own header calls out that this must match
 * `backend/app/tasks/transcription/metadata_extractor.py` — a silent field-mapping
 * bug here is the frontend mirror of the `AudioFormat`-shadowing bug fixed
 * server-side in #470, per issue #475's own framing.
 */
import { describe, it, expect } from 'vitest';
import {
  mapFFmpegMetadata,
  estimateAudioSize,
  calculateCompressionRatio,
  formatFileSize,
} from './metadataMapper';

function videoFile(
  overrides: Partial<{ name: string; size: number; type: string; lastModified: number }> = {}
) {
  const {
    name = 'clip.mp4',
    size = 1024,
    type = 'video/mp4',
    lastModified = Date.now(),
  } = overrides;
  return new File([new Uint8Array(size)], name, { type, lastModified });
}

describe('mapFFmpegMetadata', () => {
  it('maps video stream specs, including a fractional frame rate and reduced aspect ratio', () => {
    const metadata = mapFFmpegMetadata(
      {
        streams: [
          {
            codec_type: 'video',
            codec_name: 'h264',
            width: 1920,
            height: 1080,
            r_frame_rate: '30000/1001',
          },
        ],
        format: {},
      },
      videoFile()
    );

    expect(metadata.VideoCodec).toBe('h264');
    expect(metadata.VideoWidth).toBe(1920);
    expect(metadata.VideoHeight).toBe(1080);
    expect(metadata.FrameRate).toBeCloseTo(29.97, 1);
    expect(metadata.AspectRatio).toBe('16:9');
  });

  it('leaves FrameRate unset rather than Infinity for a zero-denominator frame rate', () => {
    const metadata = mapFFmpegMetadata(
      {
        streams: [
          {
            codec_type: 'video',
            codec_name: 'h264',
            width: 1920,
            height: 1080,
            r_frame_rate: '30/0',
          },
        ],
        format: {},
      },
      videoFile()
    );

    expect(metadata.FrameRate).toBeUndefined();
    expect(metadata.VideoFrameRate).toBeUndefined();
  });

  it('maps audio stream specs', () => {
    const metadata = mapFFmpegMetadata(
      {
        streams: [{ codec_type: 'audio', codec_name: 'aac', channels: 2, sample_rate: '44100' }],
      },
      videoFile()
    );

    expect(metadata.AudioFormat).toBe('aac');
    expect(metadata.AudioChannels).toBe(2);
    expect(metadata.AudioSampleRate).toBe(44100);
  });

  it('prefers the container format duration over a stream duration', () => {
    const metadata = mapFFmpegMetadata(
      {
        streams: [{ codec_type: 'video', duration: '10.0' }],
        format: { duration: '90.5' },
      },
      videoFile()
    );

    expect(metadata.Duration).toBe(90.5);
  });

  it('rejects known placeholder creation dates and falls back through the candidate field list', () => {
    const metadata = mapFFmpegMetadata(
      {
        format: {
          tags: { creation_time: '0000-00-00 00:00:00', date: '2026-01-15T10:00:00Z' },
        },
      },
      videoFile()
    );

    expect(metadata.CreateDate).toBe('2026-01-15T10:00:00Z');
  });

  it('falls back to File.lastModified when no usable creation date tag exists', () => {
    const lastModified = new Date('2026-02-01T00:00:00Z').getTime();
    const metadata = mapFFmpegMetadata({}, videoFile({ lastModified }));

    expect(metadata.CreateDate).toBe(new Date(lastModified).toISOString());
  });

  it('splits a comma-separated GPS location tag into latitude/longitude', () => {
    const metadata = mapFFmpegMetadata(
      { format: { tags: { location: '40.7128,-74.0060' } } },
      videoFile()
    );

    expect(metadata.GPSLatitude).toBe('40.7128');
    expect(metadata.GPSLongitude).toBe('-74.0060');
  });

  it('parses a real ISO 6709 location tag (signed lat/lon with no delimiter) correctly', () => {
    const metadata = mapFFmpegMetadata(
      { format: { tags: { location: '+40.6894-074.0447+002.000/' } } },
      videoFile()
    );

    expect(metadata.GPSLatitude).toBe('+40.6894');
    expect(metadata.GPSLongitude).toBe('-074.0447');
  });

  it('falls back Author to Artist when no explicit author tag exists', () => {
    const metadata = mapFFmpegMetadata({ format: { tags: { artist: 'Jane Doe' } } }, videoFile());

    expect(metadata.Author).toBe('Jane Doe');
  });

  it('picks up recognized extra tags (e.g. copyright) without overwriting an already-set field', () => {
    const metadata = mapFFmpegMetadata(
      { format: { tags: { copyright: '© 2026 Someone', title: 'My Video' } } },
      videoFile()
    );

    expect(metadata.copyright).toBe('© 2026 Someone');
    expect(metadata.Title).toBe('My Video');
  });

  it('handles a probe with no streams or tags at all, still returning basic file info', () => {
    const metadata = mapFFmpegMetadata({}, videoFile({ name: 'plain.mp4', size: 500 }));

    expect(metadata.FileName).toBe('plain.mp4');
    expect(metadata.FileSize).toBe(500);
    expect(metadata.VideoCodec).toBeUndefined();
    expect(metadata.AudioFormat).toBeUndefined();
  });
});

describe('estimateAudioSize', () => {
  it('estimates size from duration and bitrate, including the 10% container overhead', () => {
    // 60s @ 32kbps: (32000/8) * 60 * 1.1 = 264000
    expect(estimateAudioSize(60, 32)).toBe(264000);
  });

  it('defaults to a 32kbps bitrate when none is given', () => {
    expect(estimateAudioSize(60)).toBe(estimateAudioSize(60, 32));
  });
});

describe('calculateCompressionRatio', () => {
  it('computes the percentage reduction', () => {
    expect(calculateCompressionRatio(1000, 250)).toBe(75);
  });

  it('returns 0 rather than dividing by zero for an empty original file', () => {
    expect(calculateCompressionRatio(0, 0)).toBe(0);
  });

  it('clamps a negative ratio (compressed larger than original) to 0', () => {
    expect(calculateCompressionRatio(100, 200)).toBe(0);
  });

  it('clamps to 100 at most', () => {
    expect(calculateCompressionRatio(1000, 0)).toBe(100);
  });
});

describe('formatFileSize', () => {
  it('formats zero bytes as a whole "0 B"', () => {
    expect(formatFileSize(0)).toBe('0 B');
  });

  it('formats bytes without a decimal point', () => {
    expect(formatFileSize(512)).toBe('512 B');
  });

  it('formats larger sizes with one decimal place and the right unit', () => {
    expect(formatFileSize(45 * 1024 * 1024 + 300 * 1024)).toBe('45.3 MB');
    expect(formatFileSize(1024)).toBe('1.0 KB');
    expect(formatFileSize(1024 * 1024 * 1024)).toBe('1.0 GB');
  });
});
