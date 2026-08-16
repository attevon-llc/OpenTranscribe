/**
 * API client for LLM settings management
 */

import axiosInstance from '../axios';
import { get } from 'svelte/store';
import { t } from '$stores/locale';

export type LLMProvider = 'openai' | 'vllm' | 'ollama' | 'anthropic' | 'openrouter' | 'custom';

export type ConnectionStatus = 'success' | 'failed' | 'pending' | 'untested';

export interface UserLLMSettings {
  uuid: string; // UUID - primary identifier from backend
  user_id: string; // UUID
  name: string;
  provider: LLMProvider;
  model_name: string;
  base_url?: string;
  max_tokens: number;
  temperature: string;
  is_active: boolean;
  last_tested?: string;
  test_status?: ConnectionStatus;
  test_message?: string;
  has_api_key: boolean;
  is_shared?: boolean;
  shared_at?: string;
  owner_name?: string;
  owner_role?: string;
  is_own?: boolean;
  created_at: string;
  updated_at: string;
}

export interface UserLLMSettingsCreate {
  name: string;
  provider: LLMProvider;
  model_name: string;
  api_key?: string;
  base_url?: string;
  max_tokens?: number;
  temperature?: string;
  is_active?: boolean;
}

export interface UserLLMSettingsUpdate {
  name?: string;
  provider?: keyof LLMProvider;
  model_name?: string;
  api_key?: string;
  base_url?: string;
  max_tokens?: number;
  temperature?: string;
  is_active?: boolean;
  is_shared?: boolean;
}

export interface ConnectionTestRequest {
  provider: LLMProvider;
  model_name: string;
  api_key?: string;
  base_url?: string;
  config_id?: string; // For edit mode - uses stored API key
}

export interface ConnectionTestResponse {
  success: boolean;
  status: ConnectionStatus;
  message: string;
  response_time_ms?: number;
  model_info?: Record<string, unknown>;
}

export interface ProviderDefaults {
  provider: LLMProvider;
  default_model: string;
  default_base_url?: string;
  requires_api_key: boolean;
  supports_custom_url: boolean;
  max_context_length?: number;
  description: string;
}

export interface SupportedProvidersResponse {
  providers: ProviderDefaults[];
}

/**
 * A model's MEASURED reasoning off-switch capability (issue #64).
 *
 * Only `'works'` may render a user-facing control. `'absent'` means the server
 * probed the model and the "off" request was indistinguishable from not asking
 * — showing a toggle there would tell the user reasoning is off while the model
 * reasons anyway. `'unknown'` (also: a uuid missing from the map) means it was
 * never probed.
 */
export type ReasoningOffSwitch = 'unknown' | 'unsupported' | 'no_reasoning' | 'absent' | 'works';

export interface ReasoningCapability {
  off_switch: ReasoningOffSwitch;
  probeable: boolean;
  probed_at?: string | null;
  reasoning_chars_on: number;
  reasoning_chars_off: number;
  reasoning_chars_omitted: number;
  detail: string;
}

export interface UserLLMConfigurationsList {
  configurations: UserLLMSettings[];
  shared_configurations: UserLLMSettings[];
  active_configuration_id?: string; // UUID
  total: number;
  /**
   * Verdict per configuration uuid. **An absent uuid has never been probed** —
   * treat it as `'unknown'`, i.e. render no control. Keyed by config uuid for
   * convenience only; the underlying measurement belongs to the
   * (provider, endpoint, model) triple, so two configs naming the same model
   * share one verdict.
   */
  reasoning_off_switch?: Record<string, ReasoningOffSwitch>;
}

export interface SetActiveConfigRequest {
  configuration_id: string; // UUID
}

export interface LLMSettingsStatus {
  has_settings: boolean;
  active_configuration?: UserLLMSettings;
  total_configurations: number;
  using_system_default: boolean;
}

export class LLMSettingsApi {
  private static readonly BASE_PATH = '/llm-settings';

  /**
   * Get supported LLM providers with their default configurations
   */
  static async getSupportedProviders(): Promise<SupportedProvidersResponse> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/providers`);
    return response.data;
  }

  /**
   * Get status information about user's LLM settings
   */
  static async getStatus(): Promise<LLMSettingsStatus> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/status`);
    return response.data;
  }

  /**
   * Get all user's LLM configurations
   */
  static async getUserConfigurations(): Promise<UserLLMConfigurationsList> {
    const response = await axiosInstance.get(`${this.BASE_PATH}`);
    return response.data;
  }

  /**
   * Create new LLM configuration for the current user
   */
  static async createSettings(settings: UserLLMSettingsCreate): Promise<UserLLMSettings> {
    const response = await axiosInstance.post(`${this.BASE_PATH}`, settings);
    return response.data;
  }

  /**
   * Update an existing LLM configuration
   */
  static async updateSettings(
    configId: string,
    settings: UserLLMSettingsUpdate
  ): Promise<UserLLMSettings> {
    const response = await axiosInstance.put(`${this.BASE_PATH}/config/${configId}`, settings);
    return response.data;
  }

  /**
   * Delete a specific LLM configuration
   */
  static async deleteConfiguration(configId: string): Promise<{ detail: string }> {
    const response = await axiosInstance.delete(`${this.BASE_PATH}/config/${configId}`);
    return response.data;
  }

  /**
   * Delete all user's LLM configurations (revert to system defaults)
   */
  static async deleteSettings(): Promise<{ detail: string }> {
    const response = await axiosInstance.delete(`${this.BASE_PATH}/all`);
    return response.data;
  }

  /**
   * Set active LLM configuration
   */
  static async setActiveConfiguration(configId: string): Promise<UserLLMSettings> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/set-active`, {
      configuration_id: configId,
    });
    return response.data;
  }

  /**
   * Test connection to LLM provider without saving settings
   */
  static async testConnection(testRequest: ConnectionTestRequest): Promise<ConnectionTestResponse> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/test`, testRequest);
    return response.data;
  }

  /**
   * Test connection using current user's active LLM configuration
   */
  static async testCurrentSettings(): Promise<ConnectionTestResponse> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/test-current`);
    return response.data;
  }

  /**
   * Toggle sharing on a configuration
   */
  static async toggleShare(configId: string, isShared: boolean): Promise<UserLLMSettings> {
    const response = await axiosInstance.put(`${this.BASE_PATH}/config/${configId}`, {
      is_shared: isShared,
    });
    return response.data;
  }

  /**
   * Test connection using a specific configuration
   */
  static async testConfiguration(configId: string): Promise<ConnectionTestResponse> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/test-config/${configId}`);
    return response.data;
  }

  /**
   * Read the recorded reasoning off-switch verdict for a configuration.
   * Never dials the provider.
   */
  static async getReasoningCapability(configId: string): Promise<ReasoningCapability> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/config/${configId}/reasoning`);
    return response.data;
  }

  /**
   * Measure and record whether this model honours a "do not reason" request.
   * Costs three real generations against the configured endpoint, so this is
   * an explicit user action and never automatic.
   */
  static async probeReasoningCapability(configId: string): Promise<ReasoningCapability> {
    const response = await axiosInstance.post(
      `${this.BASE_PATH}/config/${configId}/reasoning-probe`
    );
    return response.data;
  }

  /**
   * Get available models from an Ollama instance
   */
  static async getOllamaModels(baseUrl: string): Promise<{
    success: boolean;
    models: Array<{
      name: string;
      size: number;
      modified_at: string;
      digest: string;
      details: Record<string, unknown>;
      display_name: string;
    }>;
    total: number;
    message: string;
  }> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/ollama/models`, {
      params: { base_url: baseUrl },
    });
    return response.data;
  }

  /**
   * Get the decrypted API key for a specific configuration
   */
  static async getConfigApiKey(configId: string): Promise<{ api_key: string | null }> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/config/${configId}/api-key`);
    return response.data;
  }

  /**
   * Get available models from an OpenAI-compatible API endpoint
   * @param baseUrl - The base URL of the API
   * @param apiKey - Optional API key (for new configs)
   * @param configId - Optional config ID (for edit mode - uses stored key)
   */
  static async getOpenAICompatibleModels(
    baseUrl: string,
    apiKey?: string,
    configId?: string
  ): Promise<{
    success: boolean;
    models: Array<{
      name: string;
      id: string;
      owned_by: string;
      created: number;
    }>;
    total: number;
    message: string;
  }> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/openai-compatible/models`, {
      params: {
        base_url: baseUrl,
        api_key: apiKey,
        config_id: configId,
      },
    });
    return response.data;
  }

  /**
   * Test the encryption system
   */
  static async testEncryption(): Promise<{ status: string; message: string }> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/encryption-test`);
    return response.data;
  }

  /**
   * Get available models from Anthropic API
   * @param apiKey - API key for Anthropic (required)
   * @param configId - Optional config ID (for edit mode - uses stored key)
   */
  static async getAnthropicModels(
    apiKey?: string,
    configId?: string
  ): Promise<{
    success: boolean;
    models: Array<{
      id: string;
      display_name: string;
      created_at: string;
      type: string;
    }>;
    total: number;
    message: string;
  }> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/anthropic/models`, {
      params: {
        api_key: apiKey,
        config_id: configId,
      },
    });
    return response.data;
  }

  /**
   * Get provider-specific default configuration
   */
  static getProviderDefaults(provider: LLMProvider | string): Partial<UserLLMSettingsCreate> {
    // Normalize legacy 'claude' to 'anthropic'
    const normalizedProvider = provider === 'claude' ? 'anthropic' : provider;

    const providerDefaults: Record<string, Partial<UserLLMSettingsCreate>> = {
      openai: {
        provider: 'openai',
        model_name: 'gpt-4o-mini',
        base_url: 'https://api.openai.com/v1',
        max_tokens: 16000, // Context window for GPT-4o-mini
        temperature: '0.3',
      },
      vllm: {
        provider: 'vllm',
        model_name: 'gpt-oss',
        base_url: 'http://localhost:8012/v1',
        max_tokens: 32768, // Typical context window for vLLM models
        temperature: '0.3',
      },
      ollama: {
        provider: 'ollama',
        model_name: 'llama3.2:latest',
        base_url: 'http://localhost:11434/v1',
        max_tokens: 128000, // Modern context window
        temperature: '0.3',
      },
      anthropic: {
        provider: 'anthropic',
        model_name: 'claude-opus-4-5-20251101',
        base_url: 'https://api.anthropic.com/v1',
        max_tokens: 200000, // Anthropic Claude context window
        temperature: '0.3',
      },
      openrouter: {
        provider: 'openrouter',
        model_name: 'anthropic/claude-3-haiku',
        base_url: 'https://openrouter.ai/api/v1',
        max_tokens: 128000, // OpenRouter typical context window
        temperature: '0.3',
      },
      custom: {
        provider: 'custom',
        model_name: '',
        base_url: '',
        max_tokens: 8192, // Default context window for custom providers
        temperature: '0.3',
      },
    };

    return providerDefaults[normalizedProvider] || {};
  }

  /**
   * Get user-friendly provider name
   */
  static getProviderDisplayName(provider: LLMProvider | string): string {
    const displayNames: Record<string, string> = {
      openai: 'OpenAI',
      vllm: 'vLLM',
      ollama: 'Ollama',
      anthropic: 'Anthropic',
      claude: 'Anthropic', // Legacy support
      openrouter: 'OpenRouter',
      custom: 'Custom Provider',
    };

    return displayNames[provider] || provider;
  }

  /**
   * Get connection status display information
   */
  static getStatusDisplay(status?: ConnectionStatus): {
    text: string;
    class: string;
    icon: string;
  } {
    const tFunc = get(t);
    switch (status) {
      case 'success':
        return {
          text: tFunc('llm.status.connected'),
          class: 'success',
          icon: '✓',
        };
      case 'failed':
        return { text: tFunc('llm.status.failed'), class: 'error', icon: '✗' };
      case 'pending':
        return {
          text: tFunc('llm.status.testing'),
          class: 'pending',
          icon: '...',
        };
      case 'untested':
      default:
        return {
          text: tFunc('llm.status.untested'),
          class: 'neutral',
          icon: '?',
        };
    }
  }
}
