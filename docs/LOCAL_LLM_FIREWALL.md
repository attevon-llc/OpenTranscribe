# Local LLM Firewall Configuration

When running vLLM or Ollama on your host machine and connecting from OpenTranscribe (which runs in Docker containers), you may need to configure your local firewall to allow the containers to reach your LLM server.

## v0.4.0 Notes

### DEPLOYMENT_MODE=lite

In `lite` deployment mode, the `celery-nlp-worker` is still started (LLM summarization is not GPU-dependent), so firewall rules for the NLP worker are still required even in lite mode.

### Organization Context

If you configure org context (Settings → LLM → Organization Context), the org context string is prepended to every LLM system prompt sent by the `celery-nlp-worker`. This increases token usage slightly but does not require any additional firewall rules — the traffic goes through the same endpoint.

## The Problem

OpenTranscribe runs inside Docker containers. When you configure an LLM endpoint like `http://localhost:8000` or `http://localhost:11434`, the container tries to connect to itself (not your host machine), causing silent failures where:
- No error is displayed
- Summaries never generate
- The LLM test may pass but actual requests fail

## Solution 1: Configure UFW Firewall (Linux)

If you're running Ubuntu or another Linux distribution with UFW (Uncomplicated Firewall), you need to allow the LLM ports through your firewall.

### Check Current Firewall Status

```bash
# Check if UFW is active and see all rules
sudo ufw status verbose

# See numbered rules (useful for managing rules)
sudo ufw status numbered
```

### Allow vLLM Port (default: 8000)

```bash
# Allow vLLM API port
sudo ufw allow 8000/tcp comment 'vLLM API'

# If using a different port (check your vLLM configuration)
sudo ufw allow 8012/tcp comment 'vLLM API'
```

### Allow Ollama Port (default: 11434)

```bash
# Allow Ollama API port
sudo ufw allow 11434/tcp comment 'Ollama API'
```

### Verify Rules Were Added

```bash
sudo ufw status verbose
```

### More Restrictive Option (Docker Network Only)

For tighter security, only allow connections from Docker's network. Use `172.16.0.0/12` to cover all Docker bridge subnets (Docker creates different subnets per compose project, e.g., `172.17.x.x`, `172.19.x.x`, `172.21.x.x`):

```bash
# Allow vLLM only from Docker containers
sudo ufw allow from 172.16.0.0/12 to any port 8000 proto tcp comment 'vLLM from Docker'

# If using a non-default port
sudo ufw allow from 172.16.0.0/12 to any port 8012 proto tcp comment 'vLLM from Docker'

# Allow Ollama only from Docker containers
sudo ufw allow from 172.16.0.0/12 to any port 11434 proto tcp comment 'Ollama from Docker'
```

> **Why not `172.17.0.0/16`?** Docker Compose assigns each project a unique bridge subnet (172.17.x.x, 172.18.x.x, etc.). Using only `172.17.0.0/16` will miss containers on other subnets. The `172.16.0.0/12` range covers all private Docker networks and is not routable from the internet.

## Solution 2: Use Host IP Address

Instead of `localhost`, use your machine's actual IP address in the LLM configuration:

1. Find your host IP:
   ```bash
   # Get your local IP address
   hostname -I | awk '{print $1}'
   ```

2. In OpenTranscribe Settings > LLM Provider, use:
   - vLLM: `http://192.168.x.x:8000/v1` (replace with your IP)
   - Ollama: `http://192.168.x.x:11434`

## Solution 3: Use Docker Bridge IP (Linux)

Docker's bridge network gateway IP (`172.17.0.1`) can reach the host:

- vLLM: `http://172.17.0.1:8000/v1`
- Ollama: `http://172.17.0.1:11434`

## Solution 4: Use host.docker.internal (All Platforms)

Use the special hostname `host.docker.internal` to reach the host machine:

- vLLM: `http://host.docker.internal:8000/v1`
- Ollama: `http://host.docker.internal:11434`

On **Docker Desktop** (macOS/Windows) this works out of the box. On **Linux with Docker Engine**, you need the `extra_hosts` configuration from Solution 5 below to enable it.

## Solution 5: Add extra_hosts to Docker Compose (Advanced)

Open WebUI uses an `extra_hosts` configuration to allow containers to reach the host machine. This can be added to OpenTranscribe's docker-compose.yml:

```yaml
services:
  backend:
    extra_hosts:
      - host.docker.internal:host-gateway

  celery-nlp-worker:
    extra_hosts:
      - host.docker.internal:host-gateway

  # Add to other celery workers as needed
```

After adding this, you can use `http://host.docker.internal:8000/v1` as your vLLM endpoint.

## Troubleshooting

### Understanding the Architecture

OpenTranscribe uses different containers for different operations:

| Operation | Container | Description |
|-----------|-----------|-------------|
| LLM Test (Settings UI) | `backend` | Tests connection via `/api/llm-settings/test` |
| Summarization | `celery-nlp-worker` | Runs actual LLM summarization tasks |
| Speaker ID (LLM) | `celery-nlp-worker` | LLM-powered speaker name suggestions |
| Transcription | `celery-worker` (GPU) | Uses local Whisper/PyAnnote — no LLM |
| Speaker embeddings | `celery-embedding-worker` | No LLM — uses OpenSearch kNN only |

**Important**: The test button in Settings tests connectivity from the `backend` container, but actual summarization runs in `celery-nlp-worker`. Both containers must be able to reach your LLM server.

### Test Connectivity from Backend Container

```bash
# Test if the backend container can reach your vLLM server
docker exec -it opentranscribe-backend curl -s http://192.168.x.x:8000/v1/models

# Test with Docker bridge IP
docker exec -it opentranscribe-backend curl -s http://172.17.0.1:8000/v1/models

# Test Ollama
docker exec -it opentranscribe-backend curl -s http://192.168.x.x:11434/api/tags
```

### Test Connectivity from Celery NLP Worker Container

**This is critical** - the celery-nlp-worker is where summarization actually runs:

```bash
# Test if the celery-nlp-worker can reach your vLLM server
docker exec -it opentranscribe-celery-nlp-worker curl -s http://192.168.x.x:8000/v1/models

# Test with Docker bridge IP
docker exec -it opentranscribe-celery-nlp-worker curl -s http://172.17.0.1:8000/v1/models

# Test Ollama
docker exec -it opentranscribe-celery-nlp-worker curl -s http://192.168.x.x:11434/api/tags
```

If the backend test passes but celery-nlp-worker fails, this explains why the Settings test works but summarization doesn't.

### Check Celery Worker Logs During Summarization

```bash
# Watch celery-nlp-worker logs in real-time while triggering a summary
./opentr.sh logs celery-nlp-worker -f

# Filter for LLM-related messages
./opentr.sh logs celery-nlp-worker | grep -i "llm\|summary\|user\|config"

# Check for "No LLM provider configured" messages
./opentr.sh logs celery-nlp-worker | grep -i "not_configured\|no llm\|skipping"
```

### Check if LLM Server is Running

```bash
# Check vLLM from host
curl http://localhost:8000/v1/models

# Check Ollama from host
curl http://localhost:11434/api/tags

# If LLM is on another machine, test from host first
curl http://192.168.x.x:8000/v1/models
```

### Check Firewall is Allowing Connections

```bash
# List all UFW rules
sudo ufw status numbered

# Check if specific port is open
sudo ufw status | grep 8000
```

### Verify User LLM Settings Are Being Loaded

The summarization task loads LLM settings from the database. Check the logs for:

```bash
# Look for user settings loading messages
./opentr.sh logs celery-nlp-worker | grep -i "user.*settings\|active.*config\|create.*from"
```

Expected log messages when working:
- `Created LLMService for user X: vllm/model-name`
- `Using LLM: vllm/model-name`

Problem indicators:
- `No active LLM configuration for user X`
- `No LLM provider configured - skipping AI summary generation`

## Known Issues

### Test Passes But Summarization Fails (No Connection Attempts)

**Symptom**: The "Test Connection" button in Settings shows success, but when you trigger summarization, your LLM server shows no incoming connection attempts.

**Cause**: The test runs in the `backend` container, but summarization runs in `celery-nlp-worker`. These containers may have different network access.

**Debug Steps**:
1. Test connectivity from both containers (see commands above)
2. Check celery-nlp-worker logs for "not_configured" or "skipping" messages
3. Verify the `extra_hosts` configuration is applied to all relevant containers

### Silent Failures (No Error Shown)

**Symptom**: Summarization doesn't work, but no error is displayed in the UI.

**Cause**: If the LLM service fails to initialize (e.g., settings not found), the task may silently skip summarization and set status to "not_configured" without showing an error.

**Debug Steps**:
1. Check celery-nlp-worker logs during summarization
2. Look for `summary_status` in the database or API response
3. Check if the file's summary status is "not_configured" vs "failed"

## After Making Changes

Restart OpenTranscribe to apply the new configuration:

```bash
./opentr.sh restart-all
```

## Related Issues

- [GitHub Issue #100](https://github.com/davidamacey/OpenTranscribe/issues/100) - vLLM summaries not working
