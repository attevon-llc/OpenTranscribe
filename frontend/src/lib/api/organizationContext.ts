/**
 * API service for organization context settings
 */

import axiosInstance from '$lib/axios';

export interface OrganizationContextSettings {
  context_text: string;
  include_in_default_prompts: boolean;
  include_in_custom_prompts: boolean;
  is_shared: boolean;
  using_shared_from: string | null;
}

export interface OrganizationContextUpdate {
  context_text?: string;
  include_in_default_prompts?: boolean;
  include_in_custom_prompts?: boolean;
  is_shared?: boolean;
}

export interface SharedOrganizationContext {
  user_id: string;
  owner_name: string;
  owner_role: string;
  context_text: string;
  is_active: boolean;
}

export interface SharedOrganizationContextList {
  shared_contexts: SharedOrganizationContext[];
}

export async function getOrganizationContext(): Promise<OrganizationContextSettings> {
  const response = await axiosInstance.get('/user-settings/organization-context');
  return response.data;
}

export async function updateOrganizationContext(
  settings: OrganizationContextUpdate
): Promise<OrganizationContextSettings> {
  const response = await axiosInstance.put('/user-settings/organization-context', settings);
  return response.data;
}

export async function resetOrganizationContext(): Promise<{
  message: string;
  default_settings: OrganizationContextSettings;
}> {
  const response = await axiosInstance.delete('/user-settings/organization-context');
  return response.data;
}

export async function getSharedOrganizationContexts(): Promise<SharedOrganizationContextList> {
  const response = await axiosInstance.get('/user-settings/organization-context/shared');
  return response.data;
}

export async function useSharedOrganizationContext(
  userId: string | null
): Promise<OrganizationContextSettings> {
  const response = await axiosInstance.post('/user-settings/organization-context/use-shared', {
    user_id: userId,
  });
  return response.data;
}
