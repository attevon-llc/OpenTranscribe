/**
 * API client for ASR (Automatic Speech Recognition) provider settings management
 */

import axiosInstance from '../axios';

export type ASRProvider =
  | 'local'
  | 'deepgram'
  | 'assemblyai'
  | 'openai'
  | 'google'
  | 'azure'
  | 'aws'
  | 'speechmatics'
  | 'gladia'
  | 'pyannote';

export type ASRConnectionStatus = 'success' | 'failed' | 'pending' | 'untested';

export interface LocalWhisperModel {
  id: string;
  display_name: string;
  description: string;
  downloaded: boolean;
  supports_translation: boolean;
  language_support: string;
}

export interface ASRModelInfo {
  id: string;
  /** display_name from the backend catalog */
  display_name: string;
  /** Alias kept for backwards compatibility — populated from display_name in getProviders() */
  name: string;
  description?: string;
  price_per_min_batch?: number;
  /** price_per_min_stream from the backend catalog */
  price_per_min_realtime?: number;
  supports_diarization?: boolean;
  supports_vocabulary?: boolean;
  supports_translation?: boolean;
  languages?: number;
  /** Present for local Whisper models — whether the model weights are downloaded */
  downloaded?: boolean;
  /** Present for local Whisper models — e.g. 'multilingual' or 'english' */
  language_support?: string;
}

export interface ASRProviderInfo {
  /** id from the backend catalog — also aliased as provider */
  id: string;
  provider: ASRProvider;
  /** display_name from the backend catalog — also aliased as name */
  display_name: string;
  name: string;
  description: string;
  requires_api_key: boolean;
  /** True only for AWS — provider also needs an Access Key ID alongside the secret */
  requires_access_key_id?: boolean;
  /** requires_region from the backend catalog */
  supports_region: boolean;
  /** supports_custom_url from the backend catalog */
  supports_base_url: boolean;
  supports_diarization: boolean;
  supports_vocabulary: boolean;
  supports_translation: boolean;
  /** Not in backend catalog — defaults to true */
  sdk_available: boolean;
  /** Provider testing status: 'tested', 'experimental', or 'coming_soon' */
  status: string;
  /** Warning text shown when status is 'experimental' */
  status_note: string;
  /** Quality note about provider's built-in diarization */
  diarization_quality: string;
  models: ASRModelInfo[];
}

export interface UserASRSettingsCreate {
  name: string;
  provider: ASRProvider;
  model_name: string;
  api_key?: string;
  access_key_id?: string;
  region?: string;
  base_url?: string;
  is_active?: boolean;
}

export interface UserASRSettingsUpdate {
  name?: string;
  provider?: ASRProvider;
  model_name?: string;
  api_key?: string;
  access_key_id?: string;
  region?: string;
  base_url?: string;
  is_active?: boolean;
}

export interface UserASRSettingsResponse {
  /** Integer primary key — included in all backend responses */
  id: number;
  uuid: string;
  user_id: number;
  name: string;
  provider: ASRProvider;
  model_name: string;
  region?: string;
  base_url?: string;
  is_active: boolean;
  last_tested?: string;
  test_status?: ASRConnectionStatus;
  test_message?: string;
  has_api_key: boolean;
  /** Whether an AWS Access Key ID is already stored (encrypted, never returned) */
  has_access_key_id?: boolean;
  is_shared?: boolean;
  shared_at?: string;
  owner_name?: string;
  owner_role?: string;
  is_own?: boolean;
  created_at: string;
  updated_at: string;
}

export interface ASRSettingsList {
  configurations: UserASRSettingsResponse[];
  shared_configurations: UserASRSettingsResponse[];
  /** UUID string of the active config — use this for UUID comparisons */
  active_configuration_uuid?: string;
  /**
   * Kept for backwards compatibility. Contains the UUID string (not the integer
   * primary key) because the backend `active_config_uuid` is used as the source.
   * Prefer `active_configuration_uuid` for new code.
   */
  active_configuration_id?: string;
  total: number;
}

export interface ASRModelCapabilities {
  provider: string;
  model_id: string;
  supports_translation: boolean;
  language_support: string;
  languages: number | null;
}

export interface ASRStatusResponse {
  has_settings: boolean;
  active_config?: UserASRSettingsResponse;
  using_local_default: boolean;
  deployment_mode?: string;
  asr_configured?: boolean;
  active_provider?: string;
  active_model?: string;
  active_config_uuid?: string;
  is_cloud_provider?: boolean;
  active_model_capabilities?: ASRModelCapabilities;
}

export interface ASRConnectionTestRequest {
  provider: ASRProvider;
  model_name: string;
  api_key?: string;
  access_key_id?: string;
  region?: string;
  base_url?: string;
  config_id?: string;
}

export interface ASRConnectionTestResult {
  success: boolean;
  status?: ASRConnectionStatus;
  message: string;
  response_time_ms?: number;
  config_uuid?: string;
}

/**
 * Raw model entry as returned by the backend provider catalog, before normalisation.
 * Field names differ from the frontend `ASRModelInfo` interface (see getProviders()).
 */
interface RawASRModel {
  id: string;
  display_name?: string;
  name?: string;
  description?: string;
  price_per_min_batch?: number;
  price_per_min_realtime?: number;
  price_per_min_stream?: number;
  supports_diarization?: boolean;
  supports_vocabulary?: boolean;
  supports_translation?: boolean;
  languages?: number;
  downloaded?: boolean;
  language_support?: string;
}

/**
 * Raw provider entry as returned by the backend provider catalog, before normalisation.
 * Field names differ from the frontend `ASRProviderInfo` interface (see getProviders()).
 */
interface RawASRProvider {
  id?: string;
  provider?: string;
  display_name?: string;
  name?: string;
  description?: string;
  requires_api_key?: boolean;
  requires_access_key_id?: boolean;
  requires_region?: boolean;
  supports_region?: boolean;
  supports_custom_url?: boolean;
  supports_base_url?: boolean;
  supports_diarization?: boolean;
  supports_vocabulary?: boolean;
  supports_translation?: boolean;
  sdk_available?: boolean;
  status?: string;
  status_note?: string;
  diarization_quality?: string;
  models?: RawASRModel[];
}

export class ASRSettingsApi {
  private static readonly BASE_PATH = '/asr-settings';

  /**
   * Get all supported ASR providers with their model catalogs.
   *
   * The backend catalog uses different field names from the TypeScript interface:
   *   id          → provider (and kept as id)
   *   display_name → name (and kept as display_name)
   *   requires_region → supports_region
   *   supports_custom_url → supports_base_url
   *   price_per_min_stream → price_per_min_realtime
   *   sdk_available is absent in the backend — defaults to true
   *
   * This method normalises all of those so consumers can use either spelling.
   */
  static async getProviders(): Promise<{ providers: ASRProviderInfo[] }> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/providers`);
    const raw: RawASRProvider[] = response.data?.providers ?? [];
    const providers: ASRProviderInfo[] = raw.map((p: RawASRProvider) => {
      const providerId = p.id ?? p.provider ?? '';
      const providerName = p.display_name ?? p.name ?? providerId;
      return {
        // identity
        id: providerId,
        provider: providerId as ASRProvider,
        // display name — backend sends display_name
        display_name: providerName,
        name: providerName,
        description: p.description ?? '',
        // capability flags — normalise backend naming to frontend naming
        requires_api_key: p.requires_api_key ?? false,
        requires_access_key_id: p.requires_access_key_id ?? false,
        supports_region: p.requires_region ?? p.supports_region ?? false,
        supports_base_url: p.supports_custom_url ?? p.supports_base_url ?? false,
        supports_diarization: p.supports_diarization ?? false,
        supports_vocabulary: p.supports_vocabulary ?? false,
        supports_translation: p.supports_translation ?? false,
        // sdk_available is not in the backend catalog — assume true unless explicitly false
        sdk_available: p.sdk_available ?? true,
        // testing status and quality notes
        status: p.status ?? 'experimental',
        status_note: p.status_note ?? '',
        diarization_quality: p.diarization_quality ?? '',
        // normalise each model entry
        models: (p.models ?? []).map((m: RawASRModel) => ({
          id: m.id,
          display_name: m.display_name ?? m.name ?? m.id,
          name: m.display_name ?? m.name ?? m.id,
          description: m.description,
          price_per_min_batch: m.price_per_min_batch,
          // backend sends price_per_min_stream; interface calls it price_per_min_realtime
          price_per_min_realtime: m.price_per_min_realtime ?? m.price_per_min_stream,
          supports_diarization: m.supports_diarization,
          supports_vocabulary: m.supports_vocabulary,
          supports_translation: m.supports_translation,
          languages: m.languages,
          downloaded: m.downloaded,
          language_support: m.language_support,
        })),
      };
    });
    return { providers };
  }

  /**
   * Get all user ASR configurations.
   *
   * Backend returns:
   *   { configs: [...], active_config_id: <int|null>, active_config_uuid: <uuid|null> }
   *
   * Normalised to the consistent ASRSettingsList interface.
   */
  static async getSettings(): Promise<ASRSettingsList> {
    const response = await axiosInstance.get(`${this.BASE_PATH}`);
    const data = response.data;
    const configs: UserASRSettingsResponse[] = data.configs ?? data.configurations ?? [];
    const activeUuid: string | undefined =
      data.active_config_uuid ?? data.active_configuration_uuid ?? undefined;
    const sharedConfigs: UserASRSettingsResponse[] = data.shared_configs ?? [];
    return {
      configurations: configs,
      shared_configurations: sharedConfigs,
      active_configuration_uuid: activeUuid,
      // backwards-compat alias holds the UUID string (not the integer PK)
      active_configuration_id: activeUuid,
      total: configs.length,
    };
  }

  /**
   * Get ASR status (active provider, using local default, etc.)
   */
  static async getStatus(): Promise<ASRStatusResponse> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/status`);
    return response.data;
  }

  /**
   * Get a specific ASR configuration by UUID
   */
  static async getConfig(uuid: string): Promise<UserASRSettingsResponse> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/config/${uuid}`);
    return response.data;
  }

  /**
   * Get the decrypted API key for a specific configuration
   */
  static async getApiKey(uuid: string): Promise<{ api_key: string | null }> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/config/${uuid}/api-key`);
    return response.data;
  }

  /**
   * Create a new ASR configuration
   */
  static async createConfig(data: UserASRSettingsCreate): Promise<UserASRSettingsResponse> {
    const response = await axiosInstance.post(`${this.BASE_PATH}`, data);
    return response.data;
  }

  /**
   * Update an existing ASR configuration
   */
  static async updateConfig(
    uuid: string,
    data: UserASRSettingsUpdate
  ): Promise<UserASRSettingsResponse> {
    const response = await axiosInstance.put(`${this.BASE_PATH}/config/${uuid}`, data);
    return response.data;
  }

  /**
   * Delete a specific ASR configuration
   * Backend returns 204 No Content on success.
   */
  static async deleteConfig(uuid: string): Promise<void> {
    await axiosInstance.delete(`${this.BASE_PATH}/config/${uuid}`);
  }

  /**
   * Delete all user ASR configurations (revert to local GPU default)
   * Backend returns 204 No Content on success.
   */
  static async deleteAll(): Promise<void> {
    await axiosInstance.delete(`${this.BASE_PATH}/all`);
  }

  /**
   * Set a specific configuration as active
   */
  static async setActive(uuid: string): Promise<{ message: string; config_uuid: string }> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/set-active`, {
      config_uuid: uuid,
    });
    return response.data;
  }

  /**
   * Clear the active configuration, reverting to local GPU default.
   * Does NOT delete any saved configs — they remain available for re-activation.
   */
  static async clearActive(): Promise<{ message: string }> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/clear-active`);
    return response.data;
  }

  /**
   * Toggle sharing on an ASR configuration
   */
  static async toggleShare(uuid: string, isShared: boolean): Promise<UserASRSettingsResponse> {
    const response = await axiosInstance.put(`${this.BASE_PATH}/config/${uuid}`, {
      is_shared: isShared,
    });
    return response.data;
  }

  /**
   * Test connection to an ASR provider without saving
   */
  static async testConnection(params: ASRConnectionTestRequest): Promise<ASRConnectionTestResult> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/test`, params);
    return response.data;
  }

  /**
   * Test connection using a saved configuration
   */
  static async testSavedConfig(uuid: string): Promise<ASRConnectionTestResult> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/test-config/${uuid}`);
    return response.data;
  }

  // ---- Admin: Local Model Control ----

  /**
   * Get the active local Whisper model and available downloaded models.
   */
  static async getActiveLocalModel(): Promise<{
    active_model: string;
    source: 'database' | 'environment';
    available_models: { short_name: string; repo_id: string; downloaded: boolean }[];
    model_info?: {
      display_name: string;
      description: string;
      supports_translation: boolean;
      supports_diarization: boolean;
      language_support: string;
    };
  }> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/local-model/active`);
    return response.data;
  }

  /**
   * Set the active local Whisper model (admin only).
   */
  static async setLocalModel(modelName: string): Promise<{ model_name: string; message: string }> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/local-model/set`, {
      model_name: modelName,
    });
    return response.data;
  }

  /**
   * Restart the GPU worker to apply a new local model (admin only).
   */
  static async restartGpuWorker(): Promise<{
    status: string;
    model: string;
    workers: string[];
    active_tasks?: number;
    message: string;
  }> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/local-model/restart`);
    return response.data;
  }

  /**
   * Get user-friendly display name for a provider
   */
  static getProviderDisplayName(provider: string): string {
    const displayNames: Record<string, string> = {
      local: 'Local (GPU)',
      deepgram: 'Deepgram',
      assemblyai: 'AssemblyAI',
      openai: 'OpenAI',
      google: 'Google Cloud',
      azure: 'Azure Speech',
      aws: 'Amazon Transcribe',
      speechmatics: 'Speechmatics',
      gladia: 'Gladia',
      pyannote: 'pyannote.ai',
    };
    return displayNames[provider] || provider;
  }

  /**
   * Get CSS class for a connection status value
   */
  static getStatusColor(status: string): string {
    switch (status) {
      case 'success':
        return 'status-success';
      case 'failed':
        return 'status-error';
      case 'pending':
        return 'status-pending';
      case 'untested':
      default:
        return 'status-neutral';
    }
  }

  /**
   * Fetch the list of local Whisper models with download status.
   *
   * Reuses the /api/asr-settings/providers endpoint and extracts
   * the 'local' provider's model catalog.
   */
  static async getLocalWhisperModels(): Promise<LocalWhisperModel[]> {
    const { providers } = await this.getProviders();
    const localProvider = providers.find((p) => p.id === 'local');
    if (!localProvider) return [];
    return localProvider.models.map((m) => ({
      id: m.id,
      display_name: m.display_name ?? m.name ?? m.id,
      description: m.description ?? '',
      downloaded: m.downloaded ?? true,
      supports_translation: m.supports_translation ?? false,
      language_support: m.language_support ?? 'multilingual',
    }));
  }

  /**
   * Format price per minute as estimated hourly cost
   * e.g. 0.0043 -> "$0.26/hr"
   */
  static formatPricePerHour(pricePerMin: number): string {
    const perHour = pricePerMin * 60;
    return `$${perHour.toFixed(2)}/hr`;
  }
}

// ---- Custom Vocabulary API ----

export interface CustomVocabularyItem {
  id: number;
  user_id?: number | null;
  term: string;
  domain: string;
  category?: string;
  is_active: boolean;
  is_system: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface CustomVocabularyCreate {
  term: string;
  domain: string;
  category?: string;
}

export class CustomVocabularyApi {
  private static readonly BASE_PATH = '/custom-vocabulary';

  /**
   * Get all available vocabulary domains
   * Backend returns { domains: string[] }
   */
  static async getDomains(): Promise<string[]> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/domains`);
    return response.data.domains ?? response.data;
  }

  /**
   * Get vocabulary terms, optionally filtered by domain.
   * Backend returns { terms: [...], system_terms: [...], total, total_system }.
   * Merges user and system terms into a single list for the UI.
   */
  static async getVocabulary(domain?: string): Promise<CustomVocabularyItem[]> {
    const response = await axiosInstance.get(`${this.BASE_PATH}`, {
      params: domain && domain !== 'all' ? { domain, active_only: false } : { active_only: false },
    });
    const data = response.data;
    // Backend returns { terms, system_terms } — merge them for the UI
    if (Array.isArray(data)) return data;
    const userTerms: CustomVocabularyItem[] = data.terms ?? [];
    const systemTerms: CustomVocabularyItem[] = data.system_terms ?? [];
    return [...userTerms, ...systemTerms];
  }

  /**
   * Create a new vocabulary term
   */
  static async createTerm(data: CustomVocabularyCreate): Promise<CustomVocabularyItem> {
    const response = await axiosInstance.post(`${this.BASE_PATH}`, data);
    return response.data;
  }

  /**
   * Update an existing vocabulary term
   */
  static async updateTerm(
    id: number,
    data: Partial<CustomVocabularyCreate & { is_active: boolean }>
  ): Promise<CustomVocabularyItem> {
    const response = await axiosInstance.put(`${this.BASE_PATH}/${id}`, data);
    return response.data;
  }

  /**
   * Delete a vocabulary term
   * Backend returns 204 No Content on success.
   */
  static async deleteTerm(id: number): Promise<void> {
    await axiosInstance.delete(`${this.BASE_PATH}/${id}`);
  }

  /**
   * Bulk import vocabulary terms
   */
  static async bulkImport(
    terms: CustomVocabularyCreate[]
  ): Promise<{ created: number; skipped: number }> {
    const response = await axiosInstance.post(`${this.BASE_PATH}/bulk`, { terms });
    return response.data;
  }
}
