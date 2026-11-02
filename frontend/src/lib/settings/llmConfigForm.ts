import type {
  LLMProvider,
  LocalEndpointStatus,
  ProviderDefaults,
  UserLLMSettings,
} from '$lib/api/llmSettings';

/**
 * Issue #873: a new configuration's context window must never be a bare literal. This mirrors
 * the backend schema's own default (`UserLLMSettingsBase.max_tokens = 8192`) so an
 * uncatalogued provider still gets a sane starting point; the provider-select handler
 * overwrites it with `ProviderDefaults.max_context_length` as soon as a provider with a known
 * window is picked.
 */
export const SCHEMA_DEFAULT_MAX_TOKENS = 8192;

/** Local detection only helps for providers that run on a local server. */
export const LOCAL_CAPABLE_PROVIDERS = ['ollama', 'vllm', 'custom'];

export interface LLMFormData {
  name: string;
  // A plain string, not LLMProvider: the form is blank before a provider is picked, and stored
  // legacy values (e.g. 'claude') are still compared against in the template.
  provider: string;
  model_name: string;
  api_key: string;
  base_url: string;
  max_tokens: number;
  temperature: string;
  is_active: boolean;
  is_shared: boolean;
  aws_region: string;
  aws_access_key_id: string;
  aws_secret_access_key: string;
  aws_session_token: string;
  clear_aws_credentials: boolean;
}

export function blankForm(): LLMFormData {
  return {
    name: '',
    provider: '',
    model_name: '',
    api_key: '',
    base_url: '',
    max_tokens: SCHEMA_DEFAULT_MAX_TOKENS,
    temperature: '0.3',
    is_active: true,
    is_shared: false,
    aws_region: '',
    aws_access_key_id: '',
    aws_secret_access_key: '',
    aws_session_token: '',
    clear_aws_credentials: false,
  };
}

/** The form for editing `config`. The API key and AWS credentials are never pre-filled here. */
export function formFromConfig(config: UserLLMSettings): LLMFormData {
  return {
    name: config.name,
    provider: config.provider,
    model_name: config.model_name,
    api_key: '', // Populated afterwards if one is stored
    base_url: config.base_url || '',
    max_tokens: config.max_tokens,
    temperature: config.temperature,
    is_active: config.is_active,
    is_shared: config.is_shared || false,
    aws_region: config.aws_region || '',
    aws_access_key_id: '',
    aws_secret_access_key: '',
    aws_session_token: '',
    clear_aws_credentials: false,
  };
}

/** A key id without a secret (or the reverse) can never authenticate. */
export function isBedrockPairIncomplete(form: LLMFormData): boolean {
  return Boolean(form.aws_access_key_id.trim()) !== Boolean(form.aws_secret_access_key.trim());
}

export function isFormValid(
  form: LLMFormData,
  providerConfig: ProviderDefaults | undefined,
  editing: boolean
): boolean {
  if (!form.name.trim() || !form.provider || !form.model_name.trim()) return false;
  if (!providerConfig) return false;

  // An API key is required only for new configurations
  if (providerConfig.requires_api_key && !form.api_key.trim() && !editing) return false;

  // Providers that support custom URLs need one
  if (providerConfig.supports_custom_url && !form.base_url.trim()) return false;

  if (form.provider === 'bedrock' && isBedrockPairIncomplete(form)) return false;

  return true;
}

export function isConnectionTestValid(
  form: LLMFormData,
  providerConfig: ProviderDefaults | undefined,
  editingHasStoredKey: boolean
): boolean {
  if (!form.provider || !form.model_name) return false;
  if (form.provider === 'ollama' && !form.base_url) return false;
  if (providerConfig?.requires_api_key) {
    // In edit mode with a stored key, testing works without re-entering it
    if (editingHasStoredKey) return true;
    if (!form.api_key?.trim()) return false;
  }
  return true;
}

/**
 * The AWS fields belong to Bedrock only: every other provider's request carries none of them,
 * and a blank credential is simply not sent ("leave blank to keep it").
 */
export function awsFields(form: LLMFormData): Record<string, string> {
  if (form.provider !== 'bedrock') return {};
  const fields: Record<string, string> = {};
  const entries: Array<[string, string]> = [
    ['aws_access_key_id', form.aws_access_key_id],
    ['aws_secret_access_key', form.aws_secret_access_key],
    ['aws_session_token', form.aws_session_token],
  ];
  for (const [key, value] of entries) {
    if (value.trim()) fields[key] = value.trim();
  }
  return fields;
}

export function buildPayload(form: LLMFormData, editing: boolean) {
  const {
    aws_region,
    aws_access_key_id: _id,
    aws_secret_access_key: _secret,
    aws_session_token: _token,
    clear_aws_credentials,
    ...rest
  } = form;
  // Only a form that passed isFormValid reaches here, so `provider` is a real one.
  const base = { ...rest, provider: form.provider as LLMProvider };
  if (form.provider !== 'bedrock') return base;
  return {
    ...base,
    aws_region,
    ...awsFields(form),
    ...(editing && clear_aws_credentials ? { clear_aws_credentials: true } : {}),
  };
}

export function formatModelSize(size: number): string {
  if (size === 0) return '';
  const gb = (size / 1024 ** 3).toFixed(1);
  return `${gb}GB`;
}

/** Whether the local-endpoint discovery section shows at all. */
export function showsDiscovery(
  provider: string,
  loading: boolean,
  endpoints: LocalEndpointStatus[]
): boolean {
  return (
    (!provider || LOCAL_CAPABLE_PROVIDERS.includes(provider)) && (loading || endpoints.length > 0)
  );
}

export interface DiscoveryDisclosure {
  expanded: boolean;
  touched: boolean;
  seenProvider: string;
}

/**
 * Expanded by default only when no provider is chosen and something reachable exists; a user's
 * own toggle (`touched`) wins until the provider changes again.
 */
export function reconcileDiscovery(
  state: DiscoveryDisclosure,
  provider: string,
  endpoints: LocalEndpointStatus[]
): DiscoveryDisclosure {
  let { touched, seenProvider, expanded } = state;
  if (provider !== seenProvider) {
    seenProvider = provider;
    touched = false;
  }
  if (!touched) expanded = !provider && endpoints.some((e) => e.reachable);
  return { expanded, touched, seenProvider };
}
