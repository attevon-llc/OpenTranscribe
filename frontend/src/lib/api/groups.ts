/**
 * API service for user group management
 */
import axiosInstance from '$lib/axios';
import type {
  Group,
  GroupDetail,
  GroupCreateRequest,
  GroupUpdateRequest,
  GroupMemberAddRequest,
  GroupMemberUpdateRequest,
  GroupMember,
  UserSearchResult,
} from '$lib/types/groups';

export class GroupsApi {
  static async fetchGroups(): Promise<Group[]> {
    const response = await axiosInstance.get('/groups');
    return response.data;
  }

  static async createGroup(data: GroupCreateRequest): Promise<Group> {
    const response = await axiosInstance.post('/groups', data);
    return response.data;
  }

  static async fetchGroupDetail(uuid: string): Promise<GroupDetail> {
    const response = await axiosInstance.get(`/groups/${uuid}`);
    return response.data;
  }

  static async updateGroup(uuid: string, data: GroupUpdateRequest): Promise<Group> {
    const response = await axiosInstance.put(`/groups/${uuid}`, data);
    return response.data;
  }

  static async deleteGroup(uuid: string): Promise<void> {
    await axiosInstance.delete(`/groups/${uuid}`);
  }

  static async addMember(groupUuid: string, data: GroupMemberAddRequest): Promise<GroupMember> {
    const response = await axiosInstance.post(`/groups/${groupUuid}/members`, data);
    return response.data;
  }

  static async updateMemberRole(
    groupUuid: string,
    userUuid: string,
    data: GroupMemberUpdateRequest
  ): Promise<GroupMember> {
    const response = await axiosInstance.put(`/groups/${groupUuid}/members/${userUuid}`, data);
    return response.data;
  }

  static async removeMember(groupUuid: string, userUuid: string): Promise<void> {
    await axiosInstance.delete(`/groups/${groupUuid}/members/${userUuid}`);
  }

  static async searchUsers(query: string): Promise<UserSearchResult[]> {
    const response = await axiosInstance.get('/users/search', {
      params: { q: query },
    });
    return response.data;
  }
}
