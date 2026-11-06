# v0.7.0: changes relevant to derived deployments

For anyone who packages, extends or operates OpenTranscribe as the base of their own deployment
(custom images, an overlay of extension modules, a capability resolver, automation against the
API). This lists what changed between `v0.6.x` and `v0.7.0` at the boundaries such a deployment
touches. User-facing release notes are in `CHANGELOG.md`. The v0.6.0 equivalent is
`docs/design/v060_downstream_changes.md`.

## 1. Extension seam version: 13 -> 14

`CLOUD_SEAM_VERSION` (`backend/app/auth/constants.py`) moved from `13` to `14`. The v0.7.0 repo
cleanup split several large modules by concern. Behaviour is unchanged except for the
access-token issuer (below), but extensions that imported these names from their old modules
fail at import time until they follow the new paths. No compatibility aliases were left behind:
each name has exactly one home.

The paths an extension may rely on are now listed in
`backend/tests/fixtures/extension_surface.txt` and checked on every test run by
`backend/tests/unit/test_extension_import_surface.py`. A future move of a listed name fails that
test until the seam version is bumped and the move is recorded in the release's
downstream-changes document. If an extension imports a core name that is not on the list, ask
for it to be added rather than relying on it silently.

### Moved names

| Old path | New path | Notes |
|---|---|---|
| `app.core.constants.CeleryQueues` | `app.core.queues.CeleryQueues` | Same class. The queue-routing helpers (`gpu_preferred_queue`, the `*Priority` classes) live beside it. |
| `app.services.task_detection_service.RECOVERED_AFTER_STUCK` | `app.core.task_config.RECOVERED_AFTER_STUCK` | Same string value. |
| `app.core.constants.DEFAULT_CHAT_*` | `app.core.chat_defaults.DEFAULT_CHAT_*` | All 34 chat defaults, same names and values. |
| `app.auth.audit.query_audit_logs` | `app.auth.audit_query.query_audit_logs` | Same keyword-only signature. `app.auth.audit` keeps the writer (`AuditLogger`, `audit_logger`). |
| `app.auth.dependencies._get_client_info` | `app.api.endpoints.auth.lifecycle_gates._get_client_info` | Private helper. Extensions should call the public `app.utils.client_ip.resolve_client_ip(request)` and read the `User-Agent` header themselves. |
| `app.services.search.indexing_service._build_neural_ingest_pipeline` | `app.services.search.pipelines._build_neural_ingest_pipeline` | Private helper; only test code should touch it. |
| `app.core.security.create_access_token` | `app.auth.direct_auth.create_access_token` | **Signature changed**, see below. |

### Access-token issuer

The second access-token issuer, `app.core.security.create_access_token(subject, expires_delta,
additional_claims)`, was removed. Every login path already used
`app.auth.direct_auth.create_access_token(data, expires_delta)`, which is now the only issuer
and the documented one for extensions. To mint a token with claims of your own, put them in
`data` beside `sub`:

```python
from app.auth.direct_auth import create_access_token

token = create_access_token({"sub": str(user.uuid), **extra_claims}, expires_delta)
```

Differences from the removed issuer:

- `exp`, `iat`, `jti` and `type` are always stamped by the issuer. A value for any of them in
  `data` is ignored (the removed issuer let `additional_claims` replace them). An extension that
  relied on setting one of them must stop: the expiry is `expires_delta`, the token type is
  always `access`, and every token gets a fresh `jti` so it can be revoked on its own.
- The token no longer carries an `alg_version` claim. Nothing in core read it; the JWS header's
  `alg` says the same thing.
- The signing algorithm is `settings.JWT_ALGORITHM`, as before.

Covered by `backend/tests/unit/test_access_token_extra_claims.py`.

## 2. Adoption checklist

1. Replace each old import path in the table above with the new one.
2. Replace calls to the removed issuer with `app.auth.direct_auth.create_access_token`, moving
   `subject` into `data["sub"]` and `additional_claims` into `data`.
3. Raise the extension's expected seam version to 14.
4. Import the extension package once against the new core (for example
   `python -c "import <your_extension>"` inside the backend image) before deploying: every item
   above fails at import time, not at request time.
