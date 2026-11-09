/**
 * API service for collection sharing operations
 */
import axiosInstance from '$lib/axios';
import type {
  Share,
  ShareCreateRequest,
  ShareUpdateRequest,
  SharedCollection,
} from '$lib/types/groups';

export class SharingApi {
  static async fetchCollectionShares(collectionUuid: string): Promise<Share[]> {
    const response = await axiosInstance.get(`/collections/${collectionUuid}/shares`);
    return response.data;
  }

  static async shareCollection(collectionUuid: string, data: ShareCreateRequest): Promise<Share> {
    const response = await axiosInstance.post(`/collections/${collectionUuid}/shares`, data);
    return response.data;
  }

  static async updateSharePermission(
    collectionUuid: string,
    shareUuid: string,
    data: ShareUpdateRequest
  ): Promise<Share> {
    const response = await axiosInstance.put(
      `/collections/${collectionUuid}/shares/${shareUuid}`,
      data
    );
    return response.data;
  }

  static async revokeShare(collectionUuid: string, shareUuid: string): Promise<void> {
    await axiosInstance.delete(`/collections/${collectionUuid}/shares/${shareUuid}`);
  }

  static async fetchSharedCollections(): Promise<SharedCollection[]> {
    const response = await axiosInstance.get('/collections/shared-with-me');
    return response.data;
  }
}
