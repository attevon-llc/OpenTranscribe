/**
 * `organizationContext.ts` is a thin CRUD wrapper around
 * `/user-settings/organization-context` — these tests pin request shape and
 * response pass-through.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
  post: vi.fn(),
}));

vi.mock('../axios', () => ({ default: mockInstance }));

import {
  getOrganizationContext,
  updateOrganizationContext,
  resetOrganizationContext,
  getSharedOrganizationContexts,
  useSharedOrganizationContext,
} from './organizationContext';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('organizationContext', () => {
  it('gets the current settings', async () => {
    const settings = {
      context_text: 'Acme Corp',
      include_in_default_prompts: true,
      include_in_custom_prompts: false,
      is_shared: false,
      using_shared_from: null,
    };
    mockInstance.get.mockResolvedValue({ data: settings });

    const result = await getOrganizationContext();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/organization-context');
    expect(result).toEqual(settings);
  });

  it('PUTs a partial update', async () => {
    const updated = {
      context_text: 'New context',
      include_in_default_prompts: true,
      include_in_custom_prompts: false,
      is_shared: false,
      using_shared_from: null,
    };
    mockInstance.put.mockResolvedValue({ data: updated });

    const result = await updateOrganizationContext({ context_text: 'New context' });
    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/organization-context', {
      context_text: 'New context',
    });
    expect(result).toEqual(updated);
  });

  it('resets via DELETE', async () => {
    const resetResponse = {
      message: 'reset',
      default_settings: {
        context_text: '',
        include_in_default_prompts: true,
        include_in_custom_prompts: false,
        is_shared: false,
        using_shared_from: null,
      },
    };
    mockInstance.delete.mockResolvedValue({ data: resetResponse });

    const result = await resetOrganizationContext();
    expect(mockInstance.delete).toHaveBeenCalledWith('/user-settings/organization-context');
    expect(result).toEqual(resetResponse);
  });

  it('lists shared contexts', async () => {
    const list = {
      shared_contexts: [
        {
          user_id: 'u1',
          owner_name: 'Jane',
          owner_role: 'admin',
          context_text: 'x',
          is_active: true,
        },
      ],
    };
    mockInstance.get.mockResolvedValue({ data: list });

    const result = await getSharedOrganizationContexts();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/organization-context/shared');
    expect(result).toEqual(list);
  });

  it('adopts a shared context by owner user_id', async () => {
    const settings = {
      context_text: 'Shared text',
      include_in_default_prompts: true,
      include_in_custom_prompts: false,
      is_shared: false,
      using_shared_from: 'u1',
    };
    mockInstance.post.mockResolvedValue({ data: settings });

    const result = await useSharedOrganizationContext('u1');
    expect(mockInstance.post).toHaveBeenCalledWith(
      '/user-settings/organization-context/use-shared',
      { user_id: 'u1' }
    );
    expect(result).toEqual(settings);
  });

  it('clears the adopted shared context with a null user_id', async () => {
    const cleared = {
      context_text: '',
      include_in_default_prompts: true,
      include_in_custom_prompts: false,
      is_shared: false,
      using_shared_from: null,
    };
    mockInstance.post.mockResolvedValue({ data: cleared });

    const result = await useSharedOrganizationContext(null);
    expect(mockInstance.post).toHaveBeenCalledWith(
      '/user-settings/organization-context/use-shared',
      { user_id: null }
    );
    expect(result).toEqual(cleared);
  });
});
