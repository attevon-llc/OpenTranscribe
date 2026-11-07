import axiosInstance from '$lib/axios';

export async function getPrivateHosts(): Promise<string[]> {
  const response = await axiosInstance.get<{ hosts: string[] }>('/admin/media-private-hosts');
  return response.data.hosts;
}

export async function setPrivateHosts(hosts: string[]): Promise<string[]> {
  const response = await axiosInstance.put<{ hosts: string[] }>('/admin/media-private-hosts', {
    hosts,
  });
  return response.data.hosts;
}
