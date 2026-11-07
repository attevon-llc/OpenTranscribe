import axiosInstance from '../axios';

export interface VadDefaults {
  vad_threshold: number;
  vad_min_silence_ms: number;
  vad_min_speech_ms: number;
  vad_speech_pad_ms: number;
}

export async function getVadDefaults(): Promise<VadDefaults> {
  const response = await axiosInstance.get<VadDefaults>('/admin/transcription-defaults');
  return response.data;
}

export async function updateVadDefaults(update: Partial<VadDefaults>): Promise<VadDefaults> {
  const response = await axiosInstance.put<VadDefaults>('/admin/transcription-defaults', update);
  return response.data;
}
