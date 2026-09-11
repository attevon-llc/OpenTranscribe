# Remote ARM64 Builder - Quick Start Guide

This guide will help you set up your Mac Studio as a remote builder for **10-20x faster ARM64 Docker builds**.

## Why Do This?

Building ARM64 images on your Ubuntu server uses QEMU emulation, which is **extremely slow**:
- ❌ **Without remote builder**: 2-3 hours per build (QEMU emulation)
- ✅ **With remote builder**: 15-30 minutes per build (native Mac Studio)

## Prerequisites Checklist

### On Mac Studio
- [ ] Docker Desktop installed and running
- [ ] SSH enabled (System Settings → General → Sharing → Remote Login)
- [ ] User account you'll connect with

### On Ubuntu Server
- [ ] SSH client installed (`ssh` command available)
- [ ] Network access to Mac Studio
- [ ] Docker with buildx support

## Step-by-Step Setup (5 minutes)

### 1. Test SSH Connection

From your Ubuntu server, test SSH to Mac Studio:

```bash
# Replace 'username' with your Mac Studio username
# Use your local hostname (e.g., mac-studio.local)
ssh username@mac-studio.local "echo 'Connection OK'"

# Or use IP address if hostname doesn't resolve
ssh username@192.168.1.100 "echo 'Connection OK'"
```

**Finding your hostname:**
- On Mac Studio: Run `hostname` in Terminal
- Common format: `computername.local`
- You can also use the IP address shown in System Settings → Network

**If this fails**, set up SSH key authentication:

```bash
# Generate SSH key (if you don't have one)
ssh-keygen -t ed25519 -C "docker-buildx"

# Copy key to Mac Studio (replace with your actual hostname)
ssh-copy-id username@mac-studio.local

# Test again (should not ask for password)
ssh username@mac-studio.local "echo 'Connection OK'"
```

### 2. Run Setup Script

```bash
cd /path/to/transcribe-app

# Interactive setup
./scripts/setup-remote-builder.sh setup

# Or provide host directly (skip interactive prompt)
./scripts/setup-remote-builder.sh setup --host username@mac-studio.local
```

**What it will ask (interactive mode):**
- Remote host: `username@mac-studio.local`
  - This is where you provide your Mac Studio's hostname or IP

**What it will do:**
1. Test SSH connectivity ✓
2. Verify Docker is running on Mac Studio ✓
3. Create Docker context for remote connection ✓
4. Create multi-node buildx builder ✓
5. Bootstrap the builder ✓

### 3. Test the Configuration

```bash
./scripts/setup-remote-builder.sh test
```

This will build a small test image on both platforms to verify everything works.

### 4. Use with Your Builds

```bash
# Enable remote builder for this build
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh

# Or export for all future builds in this session
export USE_REMOTE_BUILDER=true
./scripts/docker-build-push.sh
```

## Verification

Check the builder configuration:

```bash
./scripts/setup-remote-builder.sh status
```

Expected output:
```
Name:   opentranscribe-multiarch
Driver: docker-container

Nodes:
Name:      opentranscribe-multiarch0
Platforms: linux/amd64
...

Name:      opentranscribe-multiarch1
Platforms: linux/arm64
...
```

## Daily Usage

### Building Images

```bash
# Standard multi-platform build (fast!)
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh

# Build specific component
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh backend

# Without remote builder (slower, uses QEMU)
USE_REMOTE_BUILDER=false ./scripts/docker-build-push.sh
# Or simply:
./scripts/docker-build-push.sh
```

### Checking Status

```bash
# Check builder configuration
./scripts/setup-remote-builder.sh status

# List all builders
docker buildx ls
```

## How It Works

```
┌─────────────────────────────────────────────────────┐
│ Ubuntu Server (Build Orchestrator)                  │
├─────────────────────────────────────────────────────┤
│                                                      │
│  1. Sends build context to both builders via SSH    │
│                                                      │
│  ┌────────────────────┐    ┌────────────────────┐  │
│  │ Local Builder      │    │ Remote Builder     │  │
│  │ (AMD64 native)     │    │ (ARM64 via SSH)    │  │
│  │                    │    │                    │  │
│  │ Builds AMD64       │    │ Builds ARM64       │  │
│  │ ~8-15 minutes      │    │ ~8-15 minutes      │  │
│  └────────────────────┘    └────────────────────┘  │
│            │                         │              │
│            └─────────┬───────────────┘              │
│                      ▼                              │
│         Multi-arch manifest created                 │
│         Combined image pushed to Docker Hub         │
└─────────────────────────────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────┐
        │ Mac Studio               │
        │ (Remote ARM64 Builder)   │
        ├──────────────────────────┤
        │ - Receives build context │
        │ - Compiles ARM64 natively│
        │ - Returns built layers   │
        └──────────────────────────┘

Total build time: ~15-30 minutes (parallel builds)
```

## Troubleshooting

### "Cannot connect via SSH"

```bash
# Test SSH manually
ssh username@mac-studio.local "echo test"

# Check network connectivity
ping mac-studio.local

# Verify SSH is enabled on Mac Studio
# System Settings → General → Sharing → Remote Login (ON)
```

### "Docker is not accessible on remote machine"

```bash
# On Mac Studio, verify Docker is running
docker ps

# Ensure Docker Desktop is started
```

### "Remote builder not found"

```bash
# Check if builder exists
docker buildx ls

# Recreate if needed
./scripts/setup-remote-builder.sh remove
./scripts/setup-remote-builder.sh setup
```

### Build fails with connection timeout

The remote builder may have gone to sleep. Wake it up:

```bash
docker buildx inspect opentranscribe-multiarch --bootstrap
```

### Builds still slow

Verify you're using the remote builder:

```bash
# This should show USE_REMOTE_BUILDER=true
echo $USE_REMOTE_BUILDER

# Or explicitly set it
export USE_REMOTE_BUILDER=true
```

## Performance Comparison

Real-world build times for OpenTranscribe backend:

| Build Method | AMD64 | ARM64 | Total | Speedup |
|-------------|-------|-------|-------|---------|
| **QEMU emulation** | 8-15 min | **2-3 hours** | **2-3 hours** | Baseline |
| **Remote builder** | 8-15 min | 8-15 min | **15-30 min** | **10-20x faster** |

**Bottom line:** Multi-platform builds go from ~2-3 hours to ~15-30 minutes!

## When to Use

**Use remote builder when:**
- ✅ Building multi-platform images (amd64 + arm64)
- ✅ Building for production release
- ✅ Running the complete build pipeline
- ✅ You have network access to Mac Studio

**Skip remote builder when:**
- ⏭️ Quick testing (build single platform: `PLATFORMS=linux/amd64`)
- ⏭️ Only building frontend (already fast)
- ⏭️ Mac Studio is unavailable
- ⏭️ Network is slow/unreliable

## Cleanup

To remove the remote builder configuration:

```bash
./scripts/setup-remote-builder.sh remove
```

This will:
- Remove the multi-node buildx builder
- Remove the Docker context for remote connection
- Not affect your local Docker or Mac Studio configuration

You can recreate it anytime with `./scripts/setup-remote-builder.sh setup`.

## Advanced Tips

### Permanent Configuration

Add to your `~/.bashrc` or `~/.zshrc`:

```bash
# Always use remote builder for OpenTranscribe builds
export USE_REMOTE_BUILDER=true
```

### Custom Builder Name

```bash
# Setup with custom name
./scripts/setup-remote-builder.sh setup --name my-builder

# Use it
USE_REMOTE_BUILDER=true REMOTE_BUILDER_NAME=my-builder ./scripts/docker-build-push.sh
```

### Multiple Remote Builders

You can add multiple ARM64 machines:

```bash
# Create base builder
./scripts/setup-remote-builder.sh setup --host user@mac-studio-1.local

# Add another node manually
docker buildx create \
  --name opentranscribe-multiarch \
  --append \
  --platform linux/arm64 \
  remote-arm64-2
```

### Monitoring Builds

Watch build progress in real-time:

```bash
# In one terminal, start the build
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh backend

# In another terminal, watch builder activity
watch -n 1 'docker buildx du opentranscribe-multiarch'
```

## Summary

**Before setup:**
```bash
./scripts/docker-build-push.sh
# ⏱️  Build time: 2-3 hours (QEMU emulation)
```

**After setup:**
```bash
./scripts/setup-remote-builder.sh setup
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh
# ⚡ Build time: 15-30 minutes (native builds)
```

**10-20x faster builds with 5 minutes of setup!**

## Getting Help

```bash
# Setup script help
./scripts/setup-remote-builder.sh help

# Build script help
./scripts/docker-build-push.sh help

# Check current status
./scripts/setup-remote-builder.sh status
```

## Related Documentation

- [scripts/README.md - Remote ARM64 Builder Setup](../scripts/README.md#remote-arm64-builder-setup)
- [scripts/README.md - Docker Build & Push Script](../scripts/README.md#docker-build--push-script)
- [Docker Buildx Documentation](https://docs.docker.com/buildx/working-with-buildx/)
