# scripts/pki — test PKI / mTLS tooling

Moved intact from `scripts/CLAUDE.md`.

- **PKI** — `pki/`: `setup-test-pki.sh` (generates the gitignored `test-certs/` CA + client certs;
  regenerates every client cert unconditionally each run, so callers other than
  `generate-test-env.sh` should check for existing certs first), `generate-test-env.sh` (emits
  `test-certs/pki-test.env` + `test-certs/pki-test.compose.yml` — the mechanism
  `./opentr.sh --with-pki` uses to configure PKI without ever touching `.env`; idempotent unless
  `--force-certs`), `test-pki-auth.sh` (curl-only smoke test, no browser).
  ⚠️ **`PKI_TRUSTED_PROXIES` gates IDENTITY, not just `X-Forwarded-For`.** With the default
  `pki_mode: header`, a bare `X-Client-Cert-DN` from any peer in that list authenticates with
  **no certificate at all**, and the admin DN is not a secret (`setup-test-pki.sh` hardcodes
  it). `generate-test-env.sh` writes one value as *both* `RATE_LIMIT_TRUSTED_PROXIES` and
  `PKI_TRUSTED_PROXIES`, which is how a blanket `192.168.0.0/16` — added to fix #615's silent
  trust failure — became remote unauthenticated admin impersonation on any ordinary LAN (#620).
  Three closures, none sufficient alone: the allowlist is **derived** from the compose
  project's own docker network (`--print-trusted-proxies` resolves it and exits; fallback
  `127.0.0.1/32,172.16.0.0/12` with a loud warning, self-healing on the next start once the
  network exists); the backend's published port pins to loopback via `BACKEND_BIND_HOST`
  (`--backend-bind-host` widens it, explicitly — the script deliberately does **not** read the
  ambient value, or a stock `.env` line would switch the control off silently); and the PKI
  nginx configs **clear** `X-Client-Cert`/`-Verify`/`-DN` on every backend-facing `location`
  that is not the mTLS one — the plain-HTTP listener had no `/api/auth/pki` block, so a forged
  DN reached the backend from a peer the allowlist trusts by design, which no CIDR narrowing
  could have fixed. `backend/tests/unit/test_pki_trusted_proxies_default.py` and
  `test_pki_lan_exposure_guards.py` fail on any of the three regressing.
  `start-pki-prod.sh` was deleted — superseded by `./opentr.sh start prod --build --with-pki`,
  which (unlike the old script) includes `docker-compose.local.yml` so it serves the locally
  built image rather than stale Docker Hub tags, and never greps `.env`.
