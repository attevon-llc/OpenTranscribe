---
sidebar_label: PKI / X.509 Certificates
sidebar_position: 4
---

# PKI / X.509 certificate authentication

Users authenticate with an X.509 client certificate instead of a password. Common in
government and military deployments (CAC/PIV), and anywhere mutual TLS is already in place.

Configure it at **Settings → Authentication → PKI** (super_admin); values are stored in
`auth_config` and take effect without a restart, with the `PKI_*` environment variables as a
bootstrap seed and fallback.

## How it works

```
 Client (with cert)  ──▶  Reverse proxy (mTLS termination)  ──▶  OpenTranscribe backend
        │                          │                                      │
   presents cert            validates against CA,                  re-validates,
                            forwards cert and/or DN                 authenticates
                            in headers                              or provisions
```

The reverse proxy terminates mTLS. It is what vouches for the headers it forwards — which is
why the trust configuration below is not optional.

## Trust configuration — read this first

:::danger `PKI_TRUSTED_PROXIES` is required whenever PKI is enabled
Header-sourced PKI authentication is **refused outright** when no trusted proxy is allow-listed.
A hardened deployment additionally refuses to *start*.

Previously a forwarded DN was accepted from any source with only a warning, and a DN header
alone could authenticate without a certificate ever being parsed. Set
`pki_trusted_proxies` to the address the backend sees the reverse proxy arrive from, e.g.
`127.0.0.1,10.0.0.0/8`. Single addresses and CIDR ranges are both accepted.
:::

A DN header is trusted **only** when it arrives from a configured proxy, or alongside a
certificate this process has itself validated.

`pki_mode` decides how much the proxy is trusted to do:

| `pki_mode` | Meaning |
|---|---|
| `header` *(default)* | A trusted proxy terminates mTLS and forwards the certificate and/or its DN |
| `mutual_tls` | Same transport, but a bare DN assertion is **refused** even from a trusted proxy — the full certificate must be forwarded so this application validates it itself |

:::note Changed in v0.5.0
`pki_mode` used to be `direct` / `broker` / `hybrid` in the schema and `header` / `mutual_tls`
in the admin UI, so no value could match and **every save of the PKI tab was rejected**; no
backend code branched on it either way. It is now `header` | `mutual_tls`, and `pki_auth.py`
reads it.

`pki_support_cac` and `pki_support_piv` were **removed** (migration `v375` deletes their stored
rows). They gated nothing: both the DoD CAC and the PIV CN formats are parsed for every
certificate, unconditionally, and always have been.
:::

## Configuration reference

| Field | Config key | Default |
|---|---|---|
| Enabled | `pki_enabled` | `false` |
| CA certificate path | `pki_ca_cert_path` | — |
| Certificate header | `pki_cert_header` | `X-Client-Cert` |
| DN header | `pki_cert_dn_header` | `X-Client-Cert-DN` |
| Trusted proxies | `pki_trusted_proxies` | — (**required when enabled**) |
| Mode | `pki_mode` | `header` |
| Admin DNs | `pki_admin_dns` | — |
| Verify revocation | `pki_verify_revocation` | `false` |
| Revocation soft-fail | `pki_revocation_soft_fail` | `false` |
| OCSP timeout (s) | `pki_ocsp_timeout_seconds` | `5` (1–120) |
| CRL cache (s) | `pki_crl_cache_seconds` | `3600` (1–604800) |
| Allow password fallback | `pki_allow_password_fallback` | `true` |

`pki_admin_dns` is a `|`-separated list of full subject DNs that should receive `admin`. DN
matching is exact and case-sensitive — copy the string straight out of the certificate.

### Revocation checking

Turn on `pki_verify_revocation` and OpenTranscribe checks each presented certificate:

- **OCSP** first, using the responder URL from the certificate's own Authority Information
  Access extension. Real-time; results are cached per serial with LRU eviction.
- **CRL** as the cross-check, downloaded from the certificate's CRL distribution points and
  cached for `pki_crl_cache_seconds`.

There is no separate "OCSP responder URL" or "CRL endpoint" setting — both come from the
certificate, which is what those extensions are for.

**`pki_revocation_soft_fail` decides what happens when revocation status cannot be
determined** (responder unreachable, no CRL published). It defaults to `false` — *hard fail*,
reject the certificate. Set it to `true` only if you accept that an outage at your CA makes
revoked certificates usable.

### Password fallback

`pki_allow_password_fallback` is a **deployment ceiling** over the per-user
`User.allow_local_fallback` flag: effective permission is *per-user AND this*. It defaults to
`true`, so it adds no restriction on upgrade — the per-user flag already defaults to off and
remains the precise control. Setting it to `false` turns password fallback off for every `pki`
account at once without editing them individually.

**An active `super_admin` is exempt from both.** Auth configuration is super_admin-gated, so
the account that could undo a misconfiguration must not be locked out by it. Note the exemption
only helps if that account actually *has* a password path: `auth_type='local'`, or `pki` with
`allow_local_fallback` set. Document that credential somewhere safe — in a PKI-only deployment
it is the only non-certificate way in.

### MFA

A PKI user bypasses local MFA **only when they authenticated with their certificate**. If they
fall back to a local password, MFA still applies.

## Development and testing

### API-level (no browser)

```bash
./scripts/pki/setup-test-pki.sh
```

Creates a root CA at `scripts/pki/test-certs/ca/ca.crt`, client certificates for `testuser`,
`admin`, `john.doe` and `jane.smith`, and `.p12` files for browser import (password `changeit`).

```bash
./scripts/pki/test-pki-auth.sh admin      # gets admin
./scripts/pki/test-pki-auth.sh testuser   # gets user

# or by hand, simulating what the proxy does
ADMIN_DN=$(openssl x509 -in scripts/pki/test-certs/clients/admin.crt -noout -subject | sed 's/subject=//')
curl -X POST http://localhost:5174/api/auth/pki/authenticate \
  -H "X-Client-Cert-DN: $ADMIN_DN"
```

This only works with `pki_trusted_proxies` covering the address the request arrives from.
`POST /api/auth/pki/authenticate` is rate-limited and lockout-tracked like every other auth
route — it mints real access and refresh tokens.

### Browser-based (requires nginx mTLS)

:::warning Production mode only
Browser PKI needs nginx to verify the client certificate. The Vite dev server cannot do mTLS,
so `./opentr.sh start dev` cannot be used for it.
:::

```bash
./opentr.sh start prod --build --with-pki   # build local code first
./opentr.sh start prod --with-pki           # or use published images
```

Import a `.p12` from `scripts/pki/test-certs/clients/` (password `changeit`) and open
**https://localhost:5182**.

- **macOS**:
  `security import scripts/pki/test-certs/clients/admin.p12 -k ~/Library/Keychains/login.keychain-db -P changeit -A`,
  then in Keychain Access set the private key's Access Control to allow all applications.
- **Chrome (Windows/Linux)**: Settings → Privacy and security → Security → Manage certificates
  → Import.
- **Firefox**: Settings → Privacy & Security → Certificates → View Certificates → Your
  Certificates → Import.

| Certificate | Email | Role |
|---|---|---|
| `admin.p12` | admin@example.com | admin |
| `testuser.p12` | testuser@example.com | user |
| `john.doe.p12` | john.doe@example.com | user |
| `jane.smith.p12` | jane.smith@example.com | user |

## Smart cards (CAC / PIV)

1. Install the card-reader drivers.
2. Configure the browser's PKCS#11 module.
3. Insert the card before navigating to the login page.

```bash
# Chrome on Linux
sudo apt install opensc opensc-pkcs11
# Settings → Privacy → Security → Manage certificates → Security Devices
# Add: /usr/lib/x86_64-linux-gnu/opensc-pkcs11.so
```

Both the DoD CAC and PIV common-name formats are parsed for every certificate; there is nothing
to enable.

## Account linking

A certificate whose subject DN matches no account, but whose email matches an existing one, is
only linked when the source asserts that address is verified — and **never** to a `super_admin`.
See [Account linking](./overview#account-linking).

## Production checklist

- [ ] `pki_trusted_proxies` set to the proxy address the backend sees
- [ ] `pki_mode` chosen deliberately (`mutual_tls` if a bare DN must never be enough)
- [ ] `pki_ca_cert_path` points at your enterprise CA, mounted into the backend container
- [ ] `pki_verify_revocation=true` with `pki_revocation_soft_fail=false`
- [ ] `pki_admin_dns` populated with exact subject DNs
- [ ] A documented break-glass `super_admin` credential
- [ ] Certificate renewal planned before expiry
- [ ] CA private key protected — its compromise means fraudulent certificates

## Troubleshooting

| Symptom | Check |
|---|---|
| "PKI authentication is not enabled" | Settings → Authentication → PKI. Database configuration overrides `.env` |
| Every certificate is refused with no parse attempt | `pki_trusted_proxies` is empty, so header-sourced auth is refused |
| A DN header alone is refused from a trusted proxy | `pki_mode` is `mutual_tls`; forward the full certificate |
| "Invalid or missing client certificate" | Certificate imported in the browser, proxy forwarding the headers, certificate not expired |
| "Certificate not accepted" | CA matches the issuer, validity dates, key-usage extensions |
| Valid certificate refused when the CA is unreachable | `pki_verify_revocation` is on and `pki_revocation_soft_fail` is `false` — that is the intended behaviour |

```bash
openssl verify -CAfile ca.crt user.crt   # chain
openssl x509 -in user.crt -text -noout   # details
openssl x509 -in user.crt -subject -noout # DN, for pki_admin_dns
```
