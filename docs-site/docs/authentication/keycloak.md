---
sidebar_label: Keycloak (moved)
sidebar_position: 99
title: Keycloak / OIDC — moved
---

# This page moved to [OpenID Connect](./oidc)

OpenTranscribe's OIDC support is no longer named for one vendor. The setup guide now lives at
**[Authentication → OpenID Connect](./oidc)** and covers Keycloak exactly as before, plus every
other conforming provider (Authentik, Authelia, Okta, Entra ID, Auth0, Zitadel).

Nothing you configured needs to change:

- Every `KEYCLOAK_*` environment variable keeps working, permanently.
- Stored database configuration was renamed automatically by migration
  `v377_rename_keycloak_config_to_oidc`.
- The redirect URI registered at your identity provider points at the SPA's `/login` page, so
  the backend route rename needed no identity-provider change.

See also [IdP group mapping](./groups) for driving in-app groups and roles from provider claims.
