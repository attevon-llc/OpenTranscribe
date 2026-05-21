---
description: Rebuild the frontend (and optionally backend) Docker image locally and force-recreate the container so local code changes are visible. Required before testing UI changes against the prod / nginx / pki overlays — Docker Hub images contain stale code.
---

# Rebuild Frontend (Local Image)

The user wants their local code changes visible in the running app. Production / nginx / PKI overlays serve pre-built images from Docker Hub, so a rebuild is required.

## Steps

1. **Confirm scope** — ask the user if they want frontend only, backend only, or both. Default to frontend only if `$ARGUMENTS` is empty.

2. **Rebuild image(s)**:
   ```bash
   # Frontend
   docker build -t davidamacey/opentranscribe-frontend:latest -f frontend/Dockerfile.prod frontend/

   # Backend (only if requested)
   docker build -t davidamacey/opentranscribe-backend:latest -f backend/Dockerfile.prod backend/
   ```

3. **Detect active overlays** — check `docker ps --format '{{.Names}}'` for `opentranscribe-nginx` (PKI/nginx) vs plain prod. Build the right `docker compose ... up -d --no-deps --force-recreate` invocation.

4. **Force-recreate the changed services**:
   ```bash
   docker compose \
     -f docker-compose.yml \
     -f docker-compose.prod.yml \
     -f docker-compose.local.yml \
     [-f docker-compose.nginx.yml] \
     [-f docker-compose.pki.yml] \
     up -d --no-deps --force-recreate frontend [backend]
   ```

5. **Verify** — run `docker logs --tail 50 opentranscribe-frontend` (and backend if rebuilt). Report if the container is healthy and listening.

## Notes

- Use the **content-hash trick** if a tiny code change isn't picked up: `echo "<!-- $(date +%s) -->" >> frontend/src/<some-file>.svelte`, then re-build. Don't fall back to `--no-cache` (it re-runs `npm ci`).
- `docker-compose.local.yml` sets `pull_policy: never` — required to keep the local image from being overwritten by Docker Hub.
- `docker-compose.override.yml` (Vite hot-reload) is NOT auto-loaded when explicit `-f` flags are passed.

## Arguments

`$ARGUMENTS` — optional: `frontend` (default), `backend`, or `both`.
