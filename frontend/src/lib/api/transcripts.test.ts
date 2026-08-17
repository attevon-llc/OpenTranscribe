/**
 * `updateSegmentSpeaker` is a thin PUT wrapper — the one thing worth pinning
 * beyond the request shape is that it logs and rethrows on failure rather
 * than swallowing the error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  put: vi.fn(),
}));

vi.mock('../axios', () => ({ default: mockInstance }));

import { updateSegmentSpeaker } from './transcripts';

const SEGMENT_UUID = '11111111-1111-1111-1111-111111111111';
const SPEAKER_UUID = '22222222-2222-2222-2222-222222222222';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('updateSegmentSpeaker', () => {
  it('PUTs the speaker assignment and returns the updated segment', async () => {
    const segment = {
      uuid: SEGMENT_UUID,
      start_time: 0,
      end_time: 1,
      text: 'hi',
      speaker_id: SPEAKER_UUID,
    };
    mockInstance.put.mockResolvedValue({ data: segment });

    const result = await updateSegmentSpeaker(SEGMENT_UUID, SPEAKER_UUID);
    expect(mockInstance.put).toHaveBeenCalledWith(`/transcripts/segments/${SEGMENT_UUID}/speaker`, {
      speaker_uuid: SPEAKER_UUID,
    });
    expect(result).toEqual(segment);
  });

  it('sends null to unassign a speaker', async () => {
    const unassigned = { uuid: SEGMENT_UUID, start_time: 0, end_time: 1, text: 'hi' };
    mockInstance.put.mockResolvedValue({ data: unassigned });

    const result = await updateSegmentSpeaker(SEGMENT_UUID, null);
    expect(mockInstance.put).toHaveBeenCalledWith(`/transcripts/segments/${SEGMENT_UUID}/speaker`, {
      speaker_uuid: null,
    });
    expect(result).toEqual(unassigned);
  });

  it('logs and rethrows on failure rather than swallowing the error', async () => {
    const serverError = { response: { status: 500, data: { detail: 'boom' } } };
    mockInstance.put.mockRejectedValue(serverError);
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    await expect(updateSegmentSpeaker(SEGMENT_UUID, SPEAKER_UUID)).rejects.toBe(serverError);
    expect(consoleSpy).toHaveBeenCalled();
    consoleSpy.mockRestore();
  });
});
