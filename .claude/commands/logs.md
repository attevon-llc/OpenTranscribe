---
description: Tail logs for OpenTranscribe services. Pass a service name as $ARGUMENTS (backend, frontend, postgres, celery-worker, opensearch, minio, flower, redis, nginx). With no argument, shows recent logs across all services.
---

# Service Logs

Tail logs for the running OpenTranscribe stack.

## Behavior

- If `$ARGUMENTS` is empty: run `./opentr.sh logs` and show the last ~80 lines.
- If `$ARGUMENTS` is a service name (`backend`, `frontend`, `postgres`, `celery-worker`, `opensearch`, `minio`, `flower`, `redis`, `nginx`): run `./opentr.sh logs $ARGUMENTS` with `--tail 100` and stream the recent output.
- If the user wants to follow live: invoke with `run_in_background: true` and tell them how to read updates.

## Notes

- The `./opentr.sh logs` script wraps `docker compose logs -f --tail 100 <service>`.
- For multi-service issues (e.g., a request failing): grab `backend` and `celery-worker` logs together since most async work crosses both.
- For diarization / transcription failures: `celery-worker` logs are where stack traces land.
- For UI 404s on API endpoints: this almost always means the running container has stale Docker Hub code — recommend `/rebuild-frontend` (and backend if needed) rather than digging through logs.

## Common diagnostic combos

```bash
# Frontend + backend (request flow)
./opentr.sh logs frontend & ./opentr.sh logs backend

# Backend + celery (async tasks)
./opentr.sh logs backend & ./opentr.sh logs celery-worker

# Just errors across the stack.
# Read-only cross-service inspection is the documented exception to the
# "always use ./opentr.sh" rule — reading logs can't attach you to the wrong
# database. Anything that starts, stops, builds, or execs goes through the script.
docker compose logs --since 10m | grep -iE "error|exception|traceback"
```
