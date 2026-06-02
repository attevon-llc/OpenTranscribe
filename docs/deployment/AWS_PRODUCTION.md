# AWS Production Deployment & Hardening

Guidance for running OpenTranscribe as a paid, multi-tenant SaaS on AWS behind nginx,
**and** for self-hosted ("home-label") operators. It complements the in-repo nginx config
(`frontend/nginx.conf`, `frontend/nginx-pki.conf`) and the security posture verified during
the Jan–Jun 2026 frontend audit. Items already satisfied in-repo are marked ✅; items that
are AWS-infra recommendations (outside this repo) are marked ☁️.

> Scope note: the frontend is a **static client-side SPA** (`adapter-static`). All
> authorization is enforced **server-side** in FastAPI on every endpoint — the frontend's
> `canEdit`/`canViewOriginal`/`myPermission` flags are UI hints only. Never rely on the
> client to gate data; the backend already re-checks ownership/redaction (audited).

## 1. Already solid (verified in-repo) ✅
- **No secrets in the browser bundle.** Only `VITE_API_BASE_URL`, `VITE_FLOWER_URL_PREFIX`,
  `import.meta.env.DEV` are referenced. LLM/ASR provider keys are backend-only (encrypted in DB).
- **No production source maps** (`vite.config.ts`: `sourcemap: mode !== 'production'`); minified.
- **All `{@html}` is DOMPurify-sanitized** (`src/lib/utils/sanitizeHtml.ts`, allowlist).
- **Non-root nginx container** (`frontend/Dockerfile.prod`, `USER nginx`), pinned alpine base,
  `apk upgrade`, healthcheck.
- **Security headers present** (`frontend/nginx.conf`): CSP, HSTS (1y + includeSubDomains),
  X-Frame-Options SAMEORIGIN, X-Content-Type-Options nosniff, Referrer-Policy
  strict-origin-when-cross-origin, Permissions-Policy (camera/mic/geo/payment locked down),
  `server_tokens off`, gzip, 1y immutable static caching, no-cache on index.html.
- **Dependabot** (`.github/dependabot.yml`) — grouped weekly npm/pip + github-actions, monthly docker.

## 2. Close before paid launch (in-repo, tracked)
- **CSP: drop `script-src 'unsafe-inline'` — PARTIALLY DONE, needs a prod-build verification.**
  - ✅ Done: the custom theme bootstrap was externalized from `frontend/src/app.html` to
    `frontend/static/theme.js` (`<script src="/theme.js">`, render-blocking in `<head>` so it
    still applies the theme before first paint; served identically by Vite dev and nginx prod).
  - ⚠️ Remaining blocker: `script-src 'unsafe-inline'` is still required because **SvelteKit
    injects its own inline SPA bootstrap** into the built `index.html`
    (`__sveltekit_… = {…}; kit.start(app, element)`). `script-src 'self'` alone would block it
    and break the app. The fix is to enable SvelteKit's CSP so it hashes that bootstrap:
    in `svelte.config.js` set `kit.csp = { mode: 'hash', directives: { 'script-src': ['self'],
    'style-src': ['self','unsafe-inline'], … } }`. SvelteKit then emits a `<meta http-equiv>`
    CSP containing `script-src 'self' 'sha256-<bootstrap-hash>'`.
  - Caveats to handle in that effort: (a) a `<meta>` CSP **cannot** set `frame-ancestors` — keep
    that (and the other headers) on nginx, or run the meta CSP for `script-src` only and keep the
    nginx header for `frame-ancestors`/etc.; the browser enforces the **intersection** of all
    active CSPs, so don't let the nginx header's `script-src 'self'` re-block the hashed bootstrap.
    (b) Keep `style-src 'unsafe-inline'` (Svelte scoped styles + the app.html `<style>` blocks).
  - **Verify** the lockdown with `npm run build && npm run preview` (serves the real built
    output) + a browser load checking the console for **zero CSP violations** — NOT in the Vite
    dev server (dev uses nonces, prod uses hashes). Add a CSP `report-uri`/`report-to` first.
- **`npm audit` in CI.** Add `"audit": "npm audit --audit-level=moderate"` to
  `frontend/package.json` and an `npm audit` (+ backend `pip-audit`) step to
  `.github/workflows/security-scan.yml`. Re-enable the disabled Trivy/Grype/Dockle scans on a
  scheduled or self-hosted runner (they were disabled for GitHub-runner memory reasons).
- **nginx auth rate-limiting (home-label too):** add `limit_req_zone` on `/api/auth/*` to blunt
  brute-force (the backend also has per-IP/per-user limits — defense in depth).

## 3. AWS infrastructure recommendations ☁️
- **TLS at the edge:** terminate with **ACM** on an **ALB** (or CloudFront). Keep HSTS; submit the
  domain to the HSTS preload list once stable. Redirect 80→443.
- **WAF:** put **AWS WAF** (managed rule sets: Core, Known-Bad-Inputs, SQLi, rate-based) in front
  of the ALB. This is the main thing the in-repo nginx can't provide.
- **Static assets:** serve the built SPA from **S3 + CloudFront** (the 1y-immutable hashed assets
  cache perfectly); origin-shield the API. Or keep the nginx container behind the ALB — both work.
- **Trust the proxy chain:** ensure the backend validates `X-Forwarded-Proto`/`X-Forwarded-For`
  and **rejects non-HTTPS** forwarded requests; set FastAPI/uvicorn `--proxy-headers` and the
  trusted-hosts/forwarded-allow-ips to the ALB subnet only (don't trust arbitrary clients).
- **Secrets:** AWS Secrets Manager / SSM Parameter Store → injected as env at deploy. Never bake
  into the image or the bundle. Rotate DB/MinIO/provider keys.
- **Object storage:** MinIO → **S3** (or MinIO on EBS with encryption). Presigned URLs already
  used app-wide; add an S3 lifecycle rule to expire orphaned `bulk/{job_id}.zip` exports.
- **Data:** RDS Postgres (Multi-AZ, encrypted, automated backups/PITR); ElastiCache Redis;
  OpenSearch Service (or self-managed). GPU workers on EC2 g5/g6 with the NVIDIA runtime.
- **Observability:** ALB + nginx access logs (status/latency/user) to CloudWatch/S3; CSP
  `report-uri` → a collector; the frontend error boundary (`src/routes/+error.svelte`) is
  always on, plus **optional Sentry error reporting** gated on `VITE_SENTRY_DSN`.
  - **Off by default** (the home-label default): with `VITE_SENTRY_DSN` unset, the monitoring
    hook (`frontend/src/lib/monitoring.ts`) is a no-op and the Sentry SDK is **not bundled** —
    zero impact on the default build.
  - **To enable:** install the SDK in `frontend/` (`npm i @sentry/svelte`), uncomment the lazy
    `import('@sentry/svelte')` in `monitoring.ts` (kept dynamic so it lands in its own chunk,
    fetched only when a DSN is present), set `VITE_SENTRY_DSN` (and optionally
    `VITE_SENTRY_ENVIRONMENT`, `VITE_SENTRY_TRACES_SAMPLE_RATE`) in `.env`, then rebuild the
    frontend image. The DSN is a public/ingest key — safe to ship in the bundle — but the SPA
    still carries no other secrets.
- **Network:** private subnets for backend/workers/DB; only ALB public. Security groups least-priv.

## 4. Pre-launch checklist
- [ ] CSP has no `script-src 'unsafe-inline'`; zero CSP violations in the browser console.
- [ ] `npm audit` + `pip-audit` green (moderate+) in CI; Trivy scan of prod images clean.
- [ ] WAF attached; TLS A+ (HSTS, no weak ciphers); 80→443 redirect.
- [ ] Backend rejects non-HTTPS `X-Forwarded-Proto`; `--proxy-headers` + trusted IPs set.
- [ ] Secrets only in Secrets Manager; none in image/bundle (grep the built `dist/`).
- [ ] Redaction verified server-side: a non-owner CANNOT reveal redacted content even via a
      direct `?redact=false` API call (audited; re-verify against prod).
- [ ] RDS/Redis/OpenSearch encrypted + backed up; S3 lifecycle for bulk exports.
- [ ] Access logs + error reporting flowing; alerting on 5xx / auth-failure spikes.
- [ ] Load-test large uploads (15 GB limit) and the GPU transcription queue.

## 5. Self-hosted ("home-label") quick notes
- `./opentranscribe.sh` / prod compose overlays serve the nginx image with the headers above.
- Put a reverse proxy (Caddy/Traefik/nginx) with Let's Encrypt in front for TLS; set
  `NGINX_SERVER_NAME`. The same CSP/headers apply. No WAF needed at home scale, but keep
  Dependabot + image scanning on.
