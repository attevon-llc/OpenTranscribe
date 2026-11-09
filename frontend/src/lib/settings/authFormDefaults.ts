/**
 * The values an OIDC or SAML settings form shows for a field the stored config does not carry.
 * Secrets (`oidc_client_secret`, `saml_sp_private_key`) are deliberately absent: the form holds
 * a typed secret outside its model so a placeholder can never be saved over the real one.
 */
import type { OIDCConfig, SAMLConfig } from '$lib/api/authConfig';

export const OIDC_FORM_DEFAULTS: OIDCConfig = {
  oidc_enabled: false,
  oidc_server_url: '',
  oidc_internal_url: '',
  oidc_discovery_url: '',
  oidc_realm: '',
  oidc_client_id: '',
  oidc_callback_url: '',
  oidc_admin_role: 'admin',
  oidc_roles_claim: 'realm_access.roles',
  oidc_issuer: '',
  oidc_scopes: 'openid email profile',
  oidc_allowed_groups: '',
  oidc_blocked_groups: '',
  oidc_timeout: 30,
  oidc_verify_audience: true,
  oidc_audience: '',
  oidc_use_pkce: true,
  oidc_verify_issuer: true,
};

export const SAML_FORM_DEFAULTS: Omit<SAMLConfig, 'saml_sp_private_key'> = {
  saml_enabled: false,
  saml_sp_entity_id: '',
  saml_sp_acs_url: '',
  saml_sp_sls_url: '',
  saml_sp_x509_cert: '',
  saml_idp_entity_id: '',
  saml_idp_sso_url: '',
  saml_idp_slo_url: '',
  saml_idp_x509_cert: '',
  saml_want_assertions_signed: true,
  saml_want_messages_signed: true,
  saml_sign_authn_requests: false,
  saml_email_attribute: 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress',
  saml_name_attribute: 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name',
  saml_groups_attribute: 'groups',
  saml_admin_group: '',
  saml_allowed_groups: '',
  saml_blocked_groups: '',
};
