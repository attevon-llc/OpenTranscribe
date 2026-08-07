/**
 * API client for authentication configuration management.
 */
import { axiosInstance } from '../axios';

export interface AuthConfigResponse {
  id: number;
  uuid: string;
  config_key: string;
  config_value: string | null;
  is_sensitive: boolean;
  /**
   * Whether a value is stored. A sensitive key always arrives with
   * `config_value: null` — the secret never reaches the browser — so this is the
   * only signal for rendering "a secret is configured, leave blank to keep it".
   */
  is_set: boolean;
  category: string;
  data_type: string;
  description: string | null;
  requires_restart: boolean;
  created_at: string;
  updated_at: string;
}

export interface AuthConfigAuditResponse {
  id: number;
  uuid: string;
  config_key: string;
  old_value: string | null;
  new_value: string | null;
  change_type: string;
  ip_address: string | null;
  created_at: string;
}

export interface AuthMethodTestResponse {
  success: boolean;
  message: string;
  details?: Record<string, any>;
}

export interface LDAPConfig {
  ldap_enabled: boolean;
  ldap_server: string;
  ldap_port: number;
  ldap_use_ssl: boolean;
  ldap_use_tls: boolean;
  ldap_bind_dn: string;
  ldap_bind_password?: string;
  ldap_search_base: string;
  ldap_username_attr: string;
  ldap_email_attr: string;
  ldap_name_attr: string;
  ldap_user_search_filter: string;
  ldap_timeout: number;
  ldap_admin_users: string;
  ldap_admin_groups: string;
  ldap_user_groups: string;
  ldap_recursive_groups: boolean;
  ldap_group_attr: string;
}

/** Mirrors `backend/app/schemas/auth_config.py:KeycloakConfig` — same names, same order. */
export interface KeycloakConfig {
  keycloak_enabled: boolean;
  keycloak_server_url: string;
  keycloak_internal_url: string;
  /**
   * Full `.well-known/openid-configuration` URL. When set it REPLACES the
   * realm-based endpoint construction entirely and `keycloak_realm` is ignored.
   * `<server>/realms/<realm>/protocol/openid-connect/...` is a Keycloak-only URL
   * shape; Authentik and others 404 on it (issue #353).
   */
  keycloak_discovery_url: string;
  /** Only used when no discovery URL is set. */
  keycloak_realm: string;
  keycloak_client_id: string;
  /** Never sent by the API — `null` on the wire, `is_set` carries the signal. */
  keycloak_client_secret?: string | null;
  keycloak_callback_url: string;
  keycloak_admin_role: string;
  /** Dotted path to the claim carrying group/role membership. */
  keycloak_roles_claim: string;
  /** Optional issuer override; normally taken from the discovery document. */
  keycloak_issuer: string;
  keycloak_scopes: string;
  keycloak_timeout: number;
  keycloak_verify_audience: boolean;
  keycloak_audience: string;
  keycloak_use_pkce: boolean;
  keycloak_verify_issuer: boolean;
}

export interface PKIConfig {
  pki_enabled: boolean;
  pki_ca_cert_path: string;
  pki_verify_revocation: boolean;
  pki_cert_header: string;
  pki_cert_dn_header: string;
  pki_admin_dns: string;
  pki_ocsp_timeout_seconds: number;
  pki_crl_cache_seconds: number;
  pki_revocation_soft_fail: boolean;
  pki_trusted_proxies: string;
  pki_mode: string;
  pki_allow_password_fallback: boolean;
}

export interface SessionConfig {
  jwt_access_token_expire_minutes: number;
  jwt_refresh_token_expire_days: number;
  session_idle_timeout_minutes: number;
  session_absolute_timeout_minutes: number;
  max_concurrent_sessions: number;
  concurrent_session_policy: string;
}

export class AuthConfigApi {
  static async getAllConfigs(): Promise<Record<string, AuthConfigResponse[]>> {
    const response = await axiosInstance.get('/admin/auth-config');
    return response.data;
  }

  static async getConfigByCategory(category: string): Promise<Record<string, any>> {
    const response = await axiosInstance.get(`/admin/auth-config/${category}`);
    return response.data;
  }

  static async updateCategory(category: string, config: Record<string, any>): Promise<void> {
    await axiosInstance.put(`/admin/auth-config/${category}`, config);
  }

  static async testConnection(
    category: string,
    config: Record<string, any>
  ): Promise<AuthMethodTestResponse> {
    const response = await axiosInstance.post(`/admin/auth-config/${category}/test`, config);
    return response.data;
  }

  static async getAuditLog(
    category: string,
    limit: number = 100
  ): Promise<AuthConfigAuditResponse[]> {
    const response = await axiosInstance.get(`/admin/auth-config/audit/${category}`, {
      params: { limit },
    });
    return response.data;
  }

  static async migrateFromEnv(): Promise<{ migrated_count: number }> {
    const response = await axiosInstance.post('/admin/auth-config/migrate');
    return response.data;
  }
}
