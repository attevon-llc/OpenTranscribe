/**
 * TypeScript interfaces for user groups and collection sharing.
 */

export type GroupRole = 'owner' | 'admin' | 'member';
export type PermissionLevel = 'viewer' | 'editor';
type ShareTargetType = 'user' | 'group';

export interface UserBrief {
  uuid: string;
  full_name: string | null;
  email: string;
}

export interface GroupMember {
  uuid: string;
  user_uuid: string;
  email: string;
  full_name: string | null;
  role: GroupRole;
  joined_at: string;
}

export interface Group {
  uuid: string;
  name: string;
  description: string | null;
  member_count: number;
  my_role: GroupRole;
  owner: UserBrief;
  created_at: string;
}

export interface GroupDetail extends Group {
  members: GroupMember[];
  updated_at: string;
}

export interface GroupCreateRequest {
  name: string;
  description?: string;
}

export interface GroupUpdateRequest {
  name?: string;
  description?: string;
}

export interface GroupMemberAddRequest {
  user_uuid: string;
  role?: 'admin' | 'member';
}

export interface GroupMemberUpdateRequest {
  role: 'admin' | 'member';
}

export interface Share {
  uuid: string;
  target_type: ShareTargetType;
  target_uuid: string;
  target_name: string;
  target_email: string | null;
  member_count: number | null;
  permission: PermissionLevel;
  shared_by: UserBrief;
  created_at: string;
}

export interface ShareCreateRequest {
  target_type: ShareTargetType;
  target_uuid: string;
  permission?: PermissionLevel;
}

export interface ShareUpdateRequest {
  permission: PermissionLevel;
}

export interface SharedCollection {
  uuid: string;
  name: string;
  description: string | null;
  media_count: number;
  my_permission: PermissionLevel;
  shared_by: UserBrief;
  shared_at: string;
}

export interface UserSearchResult {
  uuid: string;
  full_name: string | null;
  email: string;
}

export interface ShareTargetSearchResult {
  type: 'user' | 'group';
  uuid: string;
  name: string;
  email?: string;
  member_count?: number;
}
