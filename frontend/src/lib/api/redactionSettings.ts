/**
 * API service for content-redaction settings.
 *
 * Per-user preferences (self-serve, on by default) + admin governance policy
 * (the enforcement floor that forces categories / censored exports for all users).
 */

import axiosInstance from '../axios';

type RedactionStyle = 'label' | 'asterisks' | 'first_letter' | 'blur';

/** A user's redaction preferences. */
export interface RedactionSettings {
  enabled: boolean;
  detectors: string[];
  categories: string[];
  pii_entities: string[];
  style: RedactionStyle;
  custom_words: string[];
  allowlist: string[];
  toxicity_threshold: number;
  redact_before_llm: boolean;
  default_export_redacted: boolean;
}

export interface RedactionSettingsUpdate {
  enabled?: boolean;
  detectors?: string[];
  categories?: string[];
  pii_entities?: string[];
  style?: RedactionStyle;
  custom_words?: string[];
  allowlist?: string[];
  toxicity_threshold?: number;
  redact_before_llm?: boolean;
  default_export_redacted?: boolean;
}

/** Option lists + the admin-forced/locked set (so the UI can lock controls). */
export interface RedactionSystemDefaults {
  available_detectors: string[];
  available_categories: string[];
  available_pii_entities: string[];
  available_styles: RedactionStyle[];
  locked_categories: string[];
  export_locked: boolean;
  redact_before_llm_locked: boolean;
  profanity_languages: string[];
  pii_languages: string[];
  toxicity_languages: string[];
}

/** Admin governance policy (enforcement floor). */
export interface RedactionPolicy {
  force_pii: boolean;
  force_pii_entities: string[];
  force_toxicity: boolean;
  force_toxicity_threshold: number;
  force_profanity: boolean;
  force_custom_words: string[];
  force_export_redacted: boolean;
  force_redact_before_llm: boolean;
  pii_use_gliner: boolean;
}

export type RedactionPolicyUpdate = Partial<RedactionPolicy>;

export const DEFAULT_REDACTION_SETTINGS: RedactionSettings = {
  enabled: false,
  detectors: ['profanity', 'pii', 'toxicity'],
  categories: ['profanity', 'pii', 'toxicity', 'custom'],
  pii_entities: [
    'NAME',
    'EMAIL',
    'PHONE',
    'SSN',
    'CREDIT_CARD',
    'ADDRESS',
    'BANK_ACCOUNT',
    'IP_ADDRESS',
    'IBAN',
    'LOCATION',
    'ORGANIZATION',
  ],
  style: 'label',
  custom_words: [],
  allowlist: [],
  toxicity_threshold: 0.5,
  redact_before_llm: true,
  default_export_redacted: true,
};

// --- per-user preferences ---
export async function getRedactionSettings(): Promise<RedactionSettings> {
  const response = await axiosInstance.get('/user-settings/redaction');
  return response.data;
}

export async function updateRedactionSettings(
  settings: RedactionSettingsUpdate
): Promise<RedactionSettings> {
  const response = await axiosInstance.put('/user-settings/redaction', settings);
  return response.data;
}

export async function resetRedactionSettings(): Promise<{ message: string }> {
  const response = await axiosInstance.delete('/user-settings/redaction');
  return response.data;
}

export async function getRedactionDefaults(): Promise<RedactionSystemDefaults> {
  const response = await axiosInstance.get('/user-settings/redaction/defaults');
  return response.data;
}

// --- admin governance ---
export async function getRedactionPolicy(): Promise<RedactionPolicy> {
  const response = await axiosInstance.get('/admin/redaction-policy');
  return response.data;
}

export async function updateRedactionPolicy(
  policy: RedactionPolicyUpdate
): Promise<RedactionPolicy> {
  const response = await axiosInstance.post('/admin/redaction-policy/update', policy);
  return response.data;
}

export async function triggerRedactionReindex(onlyStale = true): Promise<{ status: string }> {
  const response = await axiosInstance.post(
    '/admin/redaction-policy/reindex',
    {},
    { params: { only_stale: onlyStale } }
  );
  return response.data;
}
