import axiosInstance from '$lib/axios';
import type { Collection, CollectionVisibility } from '$lib/types/collection';

/**
 * Collection operations that have their own route. The list/create/edit calls still live in
 * `CollectionsPanel.svelte`; visibility is here because it has a single owner-only endpoint
 * (`PATCH /collections/{uuid}/visibility`, issue #1275) and `PUT` deliberately does not accept it.
 */
export class CollectionsApi {
  /**
   * Restrict an organization collection to its named people, or open it to the whole
   * organization. 403 for anyone but its creator or an organization admin, 404 for someone who
   * cannot see it, 422 for a personal collection.
   */
  static async setVisibility(uuid: string, visibility: CollectionVisibility): Promise<Collection> {
    const response = await axiosInstance.patch(`/collections/${uuid}/visibility`, { visibility });
    return response.data;
  }
}
