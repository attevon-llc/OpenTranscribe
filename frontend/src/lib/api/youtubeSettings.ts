import axiosInstance from '../axios';

export interface YoutubeSettings {
  user_rate_limit_enabled: boolean;
  user_rate_limit_per_hour: number;
  user_rate_limit_per_day: number;
  auto_retry_enabled: boolean;
  recovery_batch_size: number;
}

export async function getYoutubeSettings(): Promise<YoutubeSettings> {
  const response = await axiosInstance.get<YoutubeSettings>('/admin/youtube-settings');
  return response.data;
}

export async function updateYoutubeSettings(
  update: Partial<YoutubeSettings>
): Promise<YoutubeSettings> {
  const response = await axiosInstance.put<YoutubeSettings>('/admin/youtube-settings', update);
  return response.data;
}
