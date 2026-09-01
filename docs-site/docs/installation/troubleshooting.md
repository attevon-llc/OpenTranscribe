---
sidebar_position: 6
---

# Troubleshooting

This guide covers common issues and their solutions when installing and running OpenTranscribe.

## Installation Issues

### Permission Errors (Model Cache)

**Symptoms**:
- `Permission denied: '/home/appuser/.cache/huggingface/hub'`
- `Permission denied: '/home/appuser/.cache/yt-dlp'`
- YouTube downloads fail with permission errors
- Models fail to download or save

**Cause**: Docker creates model cache directories with root ownership, but containers run as non-root user (UID 1000) for security.

**Solution**:
```bash
# Option 1: Automated fix (recommended)
cd opentranscribe
./scripts/fix-model-permissions.sh

# Option 2: Manual fix using Docker
docker run --rm -v ./models:/models busybox chown -R 1000:1000 /models

# Option 3: Manual fix using sudo
sudo chown -R 1000:1000 ./models
sudo chmod -R 755 ./models
```

**Verification**:
```bash
# Check directory ownership (should show UID 1000)
ls -la models/

# Test write permissions
touch models/huggingface/test.txt && rm models/huggingface/test.txt
```

:::tip[Prevention]
The latest setup script automatically creates directories with correct permissions. For new installations, use the one-line installer:
```bash
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash
```
:::

### Docker Not Found

**Symptoms**:
- `docker: command not found`
- Installation script fails

**Solution**:
```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Verify installation
docker --version
docker compose version
```

### Port Already in Use

**Symptoms**:
- `Error: bind: address already in use`
- Services fail to start

**Solution**:
```bash
# Find process using port 5173 (example)
sudo lsof -i :5173

# Kill the process
sudo kill -9 <PID>

# Or change port in .env
FRONTEND_PORT=5274  # Different port
```

## GPU Issues

### GPU Not Detected

**Symptoms**:
- Transcription uses CPU (very slow)
- `nvidia-smi` fails

**Diagnosis**:
```bash
# 1. Check NVIDIA driver
nvidia-smi

# 2. Check Docker GPU access
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi

# 3. Check OpenTranscribe logs
./opentr.sh logs celery-worker | grep -i cuda
```

**Solutions**:

**If `nvidia-smi` fails**:
```bash
# Install/reinstall NVIDIA drivers
sudo ubuntu-drivers autoinstall
sudo reboot
```

**If Docker GPU test fails**:
```bash
# Install NVIDIA Container Toolkit
sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

**If logs show CPU mode**:
```bash
# Enable GPU in .env
USE_GPU=true
TORCH_DEVICE=cuda
GPU_DEVICE_ID=0

# Restart services
./opentr.sh restart-all
```

See [GPU Setup](./gpu-setup.md) for complete installation guide.

### CUDA Out of Memory

**Symptoms**:
- `RuntimeError: CUDA out of memory`
- Transcription fails partway through
- Container crashes during processing

**Solutions**:

**1. Reduce batch size**:
```bash
# Edit .env
BATCH_SIZE=8  # or 4, 2, 1
```

**2. Use smaller model**:
```bash
# Edit .env
WHISPER_MODEL=medium  # instead of large-v2
```

**3. Reduce concurrent workers**:
```bash
# For multi-GPU scaling
GPU_SCALE_WORKERS=2  # instead of 4
```

**4. Close other GPU applications**:
```bash
# Check GPU memory usage
nvidia-smi

# Close unnecessary applications using GPU
```

### Slow GPU Performance

**Symptoms**:
- GPU utilization less than 50%
- Transcription slower than expected
- Not achieving 70x realtime speed

**Diagnosis**:
```bash
# Monitor GPU during transcription
watch -n 1 nvidia-smi
```

**Solutions**:

**1. Check compute type**:
```bash
# Edit .env
COMPUTE_TYPE=float16  # not float32
```

**2. Increase batch size** (if VRAM available):
```bash
# Edit .env
BATCH_SIZE=16  # or 32
```

**3. Verify CUDA version**:
```bash
nvidia-smi | grep "CUDA Version"
# Should be 11.8+
```

## HuggingFace / Speaker Diarization Issues

### Gated Repository Error

**Symptoms**:
- `Cannot access gated repository`
- `'NoneType' object has no attribute 'eval'`
- Speaker diarization fails

**Cause**: Model agreements not accepted or token invalid.

**Solution**:

1. **Accept BOTH model agreements**:
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)

2. **Verify token in `.env`**:
   ```bash
   grep HUGGINGFACE_TOKEN .env
   # Should show: HUGGINGFACE_TOKEN=hf_xxxxx
   ```

3. **Regenerate token if needed**:
   - Visit [https://huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
   - Create new read token
   - Update `.env`
   - Restart: `./opentr.sh restart-all`

See [HuggingFace Setup](./huggingface-setup.md) for detailed guide.

### Models Download on Every Restart

**Symptoms**:
- Models re-download after restart
- Long startup times
- Wasted bandwidth

**Cause**: Model cache not persisting.

**Solution**:
```bash
# 1. Check MODEL_CACHE_DIR in .env
grep MODEL_CACHE_DIR .env

# 2. Verify directory exists and has correct permissions
ls -la models/
sudo chown -R 1000:1000 models/

# 3. Restart services
./opentr.sh restart-all
```

## Transcription Issues

### Transcription Stuck/Hanging

**Symptoms**:
- Transcription shows "Processing..." for hours
- Progress stops at specific percentage
- No errors in logs

**Understanding the 3-stage pipeline**: Transcription uses a Celery chain with three stages: CPU preprocess (0-20%) → GPU transcribe (20-80%) → CPU postprocess (80-100%). If progress stalls, identify which stage is stuck.

**Solutions**:

**1. Check container status**:
```bash
./opentr.sh status
```

**2. Check worker logs** (look for TIMING entries to identify the stuck stage):
```bash
./opentr.sh logs celery-worker | grep -E "TIMING|preprocess|transcribe|postprocess"
```

**3. Restart worker**:
```bash
docker restart celery-worker
```

**4. Check Flower dashboard for task state**:
```bash
# http://localhost:5175/flower
# Login with FLOWER_USER/FLOWER_PASSWORD from .env
# Check queue depths and active tasks
```

**5. Full restart**:
```bash
./opentr.sh restart-all
```

### Incorrect Transcription Results

**Symptoms**:
- Poor accuracy
- Wrong language detected
- Gibberish output

**Solutions**:

**1. Use larger model**:
```bash
# Edit .env
WHISPER_MODEL=large-v2  # instead of small/medium
```

**2. Check audio quality**:
- Ensure audio is clear
- Remove background noise
- Check audio format (WAV/FLAC best, MP3 okay)

**3. Force language** (if auto-detection fails):
```bash
# In UI: Select specific language instead of "Auto-detect"
```

**4. Check model downloaded correctly**:
```bash
ls -lh models/huggingface/hub/models--Systran--faster-whisper-large-v2/
```

### Speaker Diarization Not Working

**Symptoms**:
- All speakers shown as "Speaker 1"
- No speaker separation
- Diarization skipped

**Checklist**:
- [ ] HuggingFace token configured
- [ ] Both model agreements accepted
- [ ] Speaker diarization enabled in settings
- [ ] Audio has multiple speakers
- [ ] MIN_SPEAKERS and MAX_SPEAKERS set correctly

**Solutions**:
```bash
# 1. Verify token
grep HUGGINGFACE_TOKEN .env

# 2. Check model download
ls -lh models/torch/pyannote/

# 3. Adjust speaker range for large meetings
MIN_SPEAKERS=1
MAX_SPEAKERS=30  # or 50+ for conferences

# 4. Check logs
./opentr.sh logs celery-worker | grep -i diarization
```

## Database Issues

### Database Connection Failed

**Symptoms**:
- `could not connect to server`
- Backend fails to start
- 500 errors in web UI

**Solutions**:

**1. Check database status**:
```bash
./opentr.sh status postgres
```

**2. Check database logs**:
```bash
./opentr.sh logs postgres
```

**3. Verify credentials in `.env`**:
```bash
grep POSTGRES_ .env
```

**4. Test connection**:
```bash
./opentr.sh shell postgres
psql -U postgres -l
```

**5. Reset database** (WARNING: deletes all data):
```bash
./opentr.sh reset dev
```

**6. Or just reset Postgres password if you want to retain the data:**

When
```bash
./opentranscribe.sh logs postgres
```
shows error:

     FATAL:  password authentication failed for user "postgres"

Connect to the postgres container, install nano
```bash
docker exec -it opentranscribe-postgres /bin/bash
apk add nano
```
Edit pg_hba.conf and change last line from

    host all all all scram-sha-256

to

    host all all all trust
```bash
nano /var/lib/postgresql/data/pg_hba.conf
```
Save the file and restart the container.

Reconnect and update the Postgres password to match `POSTGRES_PASSWORD` in your `.env` file:
```bash
psql -U postgres
ALTER USER postgres WITH PASSWORD 'your-password-from-env-file';
\q
```

Restart the container, reconnect, and return `pg_hba.conf` last line to the original auth value of `scram-sha-256`.

Restart OpenTranscribe:
```bash
./opentranscribe.sh restart
```

Check Postgres status:
```bash
./opentranscribe.sh logs postgres
```
### Database Schema Errors

**Symptoms**:
- `relation does not exist`
- `column does not exist`
- Migration errors

**Cause**: Database schema is managed exclusively by Alembic migrations (no `init_db.sql`). Migrations run automatically on backend startup, but may fail if the database is in an unexpected state.

**Solution**:
```bash
# For development
./opentr.sh reset dev

# For production (preserves data)
./opentr.sh shell backend
alembic upgrade head
```

**Note**: Since v0.3.3, all database bootstrapping uses Alembic migrations. If upgrading from an older version, the backend startup will auto-detect the schema version and apply pending migrations.

## Network/Upload Issues

### YouTube Download Fails

**Symptoms**:
- YouTube downloads timeout
- "Video unavailable" errors
- Stuck at "Downloading..."

**Solutions**:

**1. Check network connectivity**:
```bash
curl -I https://youtube.com
```

**2. Update yt-dlp** (auto-updated, but verify):
```bash
./opentr.sh logs celery-worker | grep yt-dlp
```

**3. Check YouTube URL is accessible**:
- Try URL in browser
- Check if video is public/unlisted (not private)
- Verify no geographic restrictions

**4. Check firewall/proxy settings**

### Upload Fails

**Symptoms**:
- Files don't upload
- Upload progress stuck at 0%
- Network errors

**Solutions**:

**1. Check file size** (max 4GB):
```bash
ls -lh your-file.mp4
```

**2. Check MinIO storage**:
```bash
./opentr.sh logs minio
docker exec -it minio df -h
```

**3. Check network**:
```bash
# Test backend connectivity
curl http://localhost:5174/api/health
```

**4. Clear browser cache and retry**

## Performance Issues

### Slow Transcription (CPU Mode)

**Expected**: 0.5-1x realtime (slower than playback)

**Solutions**:
1. Enable GPU: See [GPU Setup](./gpu-setup.md)
2. Use smaller model: `WHISPER_MODEL=small`
3. Reduce batch size: `BATCH_SIZE=1`
4. Close other applications

### High Memory Usage

**Symptoms**:
- System becomes unresponsive
- Out of memory errors
- Containers killed

**Solutions**:

**1. Reduce resource usage**:
```bash
# Edit .env
WHISPER_MODEL=medium  # instead of large-v2
BATCH_SIZE=8  # instead of 16
COMPUTE_TYPE=int8  # instead of float16
```

**2. Increase system swap**:
```bash
# Check current swap
free -h

# Add swap file (8GB example)
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

**3. Limit Docker memory**:
```bash
# Add to docker-compose.yml
services:
  celery-worker:
    mem_limit: 8g
```

## Service-Specific Issues

### OpenSearch Won't Start

**Symptoms**:
- `max virtual memory areas vm.max_map_count [65530] is too low`

**Solution**:
```bash
# Temporary fix
sudo sysctl -w vm.max_map_count=262144

# Permanent fix
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### Celery Worker Not Processing Tasks

**Symptoms**:
- Tasks queued but not processed
- Flower shows no active workers

**Solutions**:

**1. Check worker status**:
```bash
./opentr.sh logs celery-worker
```

**2. Restart worker**:
```bash
docker restart celery-worker
```

**3. Check Redis connection**:
```bash
docker exec -it redis redis-cli ping
# Should return: PONG
```

### Frontend Not Loading

**Symptoms**:
- Blank page
- 404 errors
- Connection refused

**Solutions**:

**1. Check frontend container**:
```bash
./opentr.sh status frontend
./opentr.sh logs frontend
```

**2. Verify port**:
```bash
curl http://localhost:5173
```

**3. Clear browser cache**

**4. Restart frontend**:
```bash
docker restart frontend
```

## Diagnostic Commands

### Health Check

```bash
# Check all services
./opentr.sh status

# Check specific service logs
./opentr.sh logs [backend|frontend|celery-worker|postgres|redis|minio|opensearch]

# Test backend API
curl http://localhost:5174/api/health

# Test database connection
./opentr.sh shell postgres psql -U postgres -c "SELECT 1"

# Test GPU
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

### Resource Monitoring

```bash
# Docker container stats
docker stats

# GPU monitoring
watch -n 1 nvidia-smi

# Disk usage
df -h

# Memory usage
free -h

# Check model cache size
du -sh models/
```

## Getting Help

Still having issues?

- **Documentation**: Review all docs in this site
- **GitHub Issues**: [Report bugs](https://github.com/attevon-llc/OpenTranscribe/issues)
- **Discussions**: [Ask questions](https://github.com/attevon-llc/OpenTranscribe/discussions)
- **Flower Dashboard**: http://localhost:5175/flower for task debugging
- **Logs**: Include relevant logs when asking for help

### Collecting Debug Information

When reporting issues, include:

```bash
# System information
uname -a
docker --version
docker compose version
nvidia-smi

# OpenTranscribe version
git log -1 --format="%H %s"

# Container status
./opentr.sh status

# Relevant logs (last 100 lines)
./opentr.sh logs celery-worker | tail -100

# Environment (redact secrets!)
cat .env | grep -v PASSWORD | grep -v SECRET | grep -v TOKEN
```

## Next Steps

- [Hardware Requirements](./hardware-requirements.md) - Optimize your setup
- [GPU Setup](./gpu-setup.md) - Configure GPU acceleration
- [Environment Variables](../configuration/environment-variables.md) - Fine-tune settings
- [FAQ](../faq.md) - Frequently asked questions
