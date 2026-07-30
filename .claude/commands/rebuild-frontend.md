---
description: Rebuild the frontend (and optionally backend) Docker image locally and force-recreate the container so local code changes are visible. Required before testing UI changes against the prod / nginx / pki overlays — Docker Hub images contain stale code.
---

# Rebuild Frontend (Local Image)

The user wants their local code changes visible in the running app. Production / nginx / PKI overlays serve pre-built images from Docker Hub, so a rebuild is required.

**Prefer `./opentr.sh` over hand-assembled `docker` / `docker compose` commands.** The script derives the overlay chain; a hand-written `-f` list drifts as overlays are added.

## Steps

1. **Confirm scope** — ask the user if they want frontend only, backend only, or both. Default to frontend only if `$ARGUMENTS` is empty.

2. **Pick the right script command:**

   ```bash
   # Dev stack (Vite overlay) — rebuilds in place, leaves data services alone
   ./opentr.sh rebuild-frontend
   ./opentr.sh rebuild-backend [--nas]

   # Prod / nginx / PKI overlays — builds backend + frontend + docs from Dockerfile.prod,
   # then starts prod with the correct overlay chain
   ./opentr.sh start prod --build
   ./opentr.sh start prod --build --with-pki
   ```

   `./opentr.sh start prod --build` is the scripted equivalent of building the prod images by hand, and it also builds the **docs** image — easy to miss otherwise.

3. **Only if the user needs a surgical recreate** (leave postgres / minio / opensearch running — the one case the script doesn't cover), detect active overlays via `docker ps --format '{{.Names}}'` (`opentranscribe-nginx` ⇒ nginx/PKI) and run:

   ```bash
   docker compose \
     -f docker-compose.yml \
     -f docker-compose.prod.yml \
     -f docker-compose.local.yml \
     [-f docker-compose.nginx.yml] \
     [-f docker-compose.pki.yml] \
     up -d --no-deps --force-recreate frontend [backend]
   ```

   If this comes up repeatedly, add a flag to `opentr.sh` rather than spreading raw compose invocations through the docs.

4. **Verify** — run `./opentr.sh logs frontend` (and `backend` if rebuilt). Report whether the container is healthy and listening.

## Notes

- Use the **content-hash trick** if a tiny code change isn't picked up: `echo "<!-- $(date +%s) -->" >> frontend/src/<some-file>.svelte`, then re-build. Don't fall back to `--no-cache` (it re-runs `npm ci`).
- `docker-compose.local.yml` sets `pull_policy: never` — required to keep the local image from being overwritten by Docker Hub.
- `docker-compose.override.yml` (Vite hot-reload) is NOT auto-loaded when explicit `-f` flags are passed.
- Never run `./opentranscribe.sh update` against local builds — it pulls from Docker Hub.

## Arguments

`$ARGUMENTS` — optional: `frontend` (default), `backend`, or `both`.
