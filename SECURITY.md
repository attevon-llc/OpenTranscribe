# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Use GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/attevon-llc/OpenTranscribe/security/advisories/new)

That channel is private between you and the maintainers, so a fix can ship
before the details are public.

We aim to acknowledge a report within **5 working days**, and to agree a
disclosure timeline with you once the impact is understood. If you would like
credit in the advisory, say so in the report — we are glad to give it.

## Supported versions

Security fixes land on the **latest released minor version**, shipped as a
`0.x.y` patch release cut from that minor's `release/<major>.<minor>` branch —
see [Cutting a patch
release](https://attevon-llc.github.io/OpenTranscribe/docs/developer-guide/releasing#cutting-a-patch-release)
for the mechanism. **Older minors are not patched.** There is no branch and no
tooling for backporting a fix past the latest minor once a newer one exists —
if you are running an older minor, upgrading to the latest is the supported
path to a fix, not a patch on the version you already have. OpenTranscribe is
self-hosted, so upgrading is under your control: see
[Upgrading](https://attevon-llc.github.io/OpenTranscribe/docs/operations/upgrading).

## What is in scope

This is a self-hosted application that ingests user-supplied media and runs
authentication, so the areas most worth your attention are:

- Authentication and session handling (local, LDAP, OIDC, SAML, PKI, proxy, MFA)
- Authorization — anything that lets one user reach another user's files,
  transcripts, speakers, or collections
- File upload and the media processing pipeline (uploads are attacker-controlled
  input by definition)
- SSRF in the URL-ingest, watch-source, and LLM/ASR provider paths
- Secret handling and anything that writes credentials to logs or the database
- Container escape or privilege escalation from the worker containers

## What is out of scope

- **Known CVEs in base-image OS packages with no upstream fix.** We scan every
  release with Trivy, Grype and Dockle, and accepted risks are recorded with a
  reason. See the open security issues before reporting one.
- Findings from an automated scanner with no demonstrated impact on this
  application.
- Missing hardening headers or rate limits on a deployment the reporter
  configured themselves — self-hosted deployments own their own configuration.
- Social engineering, physical access, or attacks requiring an already-compromised
  host.

## For operators

If you run OpenTranscribe, the deployment-side essentials are:

- Set a strong `REDIS_PASSWORD` and never run production with default secrets —
  the app refuses to start with placeholder values when `ENVIRONMENT=production`
  (the default).
- Keep infrastructure ports (Postgres, Redis, MinIO, OpenSearch) bound to
  loopback or a private network; only the frontend needs to be reachable.
- `ENABLE_API_DOCS` is off by default on purpose: Swagger is anonymously
  reachable wherever it is mounted and enumerates the whole admin surface.

Details: [Deployment configuration](https://attevon-llc.github.io/OpenTranscribe/docs/operations/deployment-configuration).
