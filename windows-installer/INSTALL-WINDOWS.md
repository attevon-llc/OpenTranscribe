# OpenTranscribe Windows Installation Guide

Complete guide for building and installing OpenTranscribe on Windows systems. This guide covers the entire process from building the package on Linux to creating and running the Windows installer.

## Table of Contents

1. [Overview](#overview)
2. [Directory Contents](#directory-contents)
3. [Build Process (Linux)](#build-process-linux)
4. [Transfer to Windows](#transfer-to-windows)
5. [Prerequisites Setup (Windows)](#prerequisites-setup-windows)
6. [Create Installer (Windows)](#create-installer-windows)
7. [Installation](#installation)
8. [Post-Installation](#post-installation)
9. [Troubleshooting](#troubleshooting)
10. [Maintenance and Updates](#maintenance-and-updates)

---

## Overview

OpenTranscribe for Windows uses a three-phase installation approach:

1. **Build Phase (Linux)**: Collect Docker images, AI models, and configuration files
2. **Transfer Phase**: Move the complete package to Windows (USB, network, or cloud)
3. **Package Phase (Windows)**: Use Inno Setup to create a Windows installer executable

This approach allows for completely offline installation on air-gapped Windows systems.

### Package Contents

- Docker container images (~10-30GB)
- AI models for transcription (~5-40GB depending on model selection)
- Database schema and initialization scripts
- Configuration files and environment templates
- Windows batch scripts for startup and management
- Comprehensive documentation

### Total Size

- Uncompressed: 50-80GB
- Compressed (tar.xz): 15-25GB
- Final Installer: 50-80GB (Inno Setup installer)

---

## Directory Contents

The `windows-installer/` directory contains all source files for creating the Windows installer:

### Core Installer Files

- **`installer.iss`** - Inno Setup script (defines installer behavior, files to include, shortcuts)
- **`run_opentranscribe.bat`** - Startup script (loads images, detects GPU, starts services)
- **`uninstall_opentranscribe.bat`** - Cleanup script (removes containers, images, volumes)
- **`check-prerequisites.ps1`** - PowerShell prerequisite checker (verifies Docker, WSL, GPU)

### Assets and Documentation

- **`ot-icon.ico`** - Application icon
- **`license.txt`** - Software license and attributions
- **`preinstall.txt`** - Pre-installation information
- **`after-install.txt`** - Post-installation guide
- **`README-WINDOWS.md`** - End-user documentation (included in installer)
- **`INSTALL-WINDOWS.md`** - This file (developer/builder guide)

### Quick Start for Developers

**Build the package (Linux):**
```bash
export HUGGINGFACE_TOKEN="your_token_here"
./scripts/build-windows-installer.sh
# Output: offline-package-build/windows/opentranscribe-windows-v[VERSION]/
```

**Create the installer (Windows):**
```powershell
# 1. Install Inno Setup from https://jrsoftware.org/isdl.php
# 2. Update BuildDir path in installer.iss (line ~16)
# 3. Open installer.iss in Inno Setup Compiler
# 4. Build → Compile
# Output: [BuildDir]\output\OpenTranscribe-Setup-1.0.0.exe
```

---

## Build Process (Linux)

### Prerequisites

- Linux system (Ubuntu 20.04+ recommended)
- Docker installed and running
- Git repository cloned
- 100GB+ free disk space
- Internet connection (for downloading models and images)
- HuggingFace account and token (for speaker diarization models)

### Step 1: Set Environment Variables

```bash
# Required: HuggingFace token for model downloads
export HUGGINGFACE_TOKEN="your_token_here"

# Optional: Specify Whisper model size (default: large-v3-turbo)
export WHISPER_MODEL="large-v3-turbo"  # or large-v3, large-v2, medium, small, base, tiny
```

Get your HuggingFace token: https://huggingface.co/settings/tokens

### Step 2: Run Build Script

```bash
cd /path/to/OpenTranscribe

# Build with latest Docker images from Docker Hub
./scripts/build-windows-installer.sh

# OR: Build with locally built images (for testing)
./scripts/build-windows-installer.sh --local

# OR: Specify custom version
./scripts/build-windows-installer.sh v2.0.0
```

### Step 3: Wait for Completion

The build process takes 1-2 hours and includes:

1. **Pre-flight checks** - Verify Docker, disk space, required files
2. **Docker image extraction** - Identify images from docker-compose.yml
3. **GPU/Model selection** - Choose Whisper model for target system
4. **Image download** - Pull and save all Docker images (~15-30 minutes)
5. **Model download** - Download AI models using GPU (~30-60 minutes)
6. **Configuration** - Copy config files, .env templates, documentation
7. **Finalization** - Create checksums, package metadata

### Step 4: Verify Build Output

After successful build:

```
offline-package-build/
├── linux/                              # Linux offline packages (if built)
│   └── opentranscribe-offline-v[VERSION]/
└── windows/                            # Windows installer packages
    └── opentranscribe-windows-v[VERSION]/
    ├── docker-images/          # Docker image tar files
    │   ├── *.tar               # Individual image files
    │   ├── checksums.sha256    # Image checksums
    │   └── metadata.json       # Build metadata
    ├── models/                 # AI model cache
    │   ├── huggingface/        # WhisperX models
    │   ├── torch/              # PyAnnote models
    │   ├── nltk_data/          # NLTK data
    │   └── sentence-transformers/
    ├── config/                 # Configuration files
    │   ├── docker-compose.offline.yml
    │   ├── .env.example
    │   └── nginx.conf
    ├── database/               # Database init scripts
    │   └── init_db.sql
    ├── .env                    # Environment configuration
    ├── installer.iss           # Inno Setup script
    ├── run_opentranscribe.bat  # Startup script
    ├── uninstall_opentranscribe.bat
    ├── check-prerequisites.ps1  # Prerequisite checker
    ├── ot-icon.ico             # Application icon
    ├── license.txt
    ├── preinstall.txt
    ├── after-install.txt
    ├── README-WINDOWS.md       # User documentation
    ├── package-info.json       # Build metadata
    └── checksums.sha256        # Package checksums
```

---

## Transfer to Windows

### Option 1: USB Drive (Recommended for Offline)

1. **Format USB drive**
   - Use exFAT or NTFS format (NOT FAT32 - files exceed 4GB limit)
   - Requires 80GB+ free space

2. **Copy entire folder**
   ```bash
   # On Linux
   cp -r windows-installer-build/opentranscribe-windows-v[VERSION] /media/usb/
   ```

3. **Verify transfer**
   - Check file sizes match source
   - Verify checksums if possible

### Option 2: Network Transfer

```bash
# Option A: SMB/CIFS share
smbclient //WINDOWS-PC/Share -U username
put -r offline-package-build/windows/opentranscribe-windows-v[VERSION]

# Option B: SCP/SFTP to Windows
scp -r offline-package-build/windows/opentranscribe-windows-v[VERSION] user@windows-pc:C:/

# Option C: rsync over SSH
rsync -avz --progress offline-package-build/windows/opentranscribe-windows-v[VERSION]/ user@windows-pc:/cygdrive/c/opentranscribe-build/
```

### Option 3: Cloud Storage

Upload to:
- Google Drive
- Dropbox
- OneDrive
- AWS S3
- Azure Blob Storage

Then download on Windows machine.

---

## Prerequisites Setup (Windows)

### Step 1: Install Windows Subsystem for Linux 2 (WSL 2)

Open PowerShell as Administrator:

```powershell
# Install WSL 2
wsl --install

# Set WSL 2 as default
wsl --set-default-version 2

# Restart computer if prompted
```

### Step 2: Install Docker Desktop

1. Download Docker Desktop for Windows:
   - https://www.docker.com/products/docker-desktop

2. Run installer:
   - Choose "Use WSL 2 instead of Hyper-V" (recommended)
   - Enable integration with default WSL distro
   - Restart when prompted

3. Verify installation:
   ```powershell
   docker --version
   docker info
   ```

4. Configure Docker Desktop:
   - Open Docker Desktop Settings
   - Resources → Advanced:
     - Memory: Allocate 16GB+ (more if possible)
     - CPUs: Allocate 4+ cores
     - Disk: Ensure 200GB+ available
   - General:
     - Use WSL 2 based engine: ✓ Enabled
   - Resources → WSL Integration:
     - Enable integration with your WSL distros

### Step 3: Install NVIDIA Drivers (Optional but Recommended)

For GPU-accelerated transcription:

1. Check GPU compatibility:
   - NVIDIA GPU with CUDA support
   - 8GB+ VRAM recommended (4GB minimum)

2. Download latest drivers:
   - https://www.nvidia.com/Download/index.aspx
   - Use "Game Ready Driver" or "Studio Driver"
   - Install with default options

3. Verify installation:
   ```powershell
   nvidia-smi
   ```

4. Enable CUDA in Docker Desktop:
   - Settings → Resources → WSL Integration
   - Check "Enable integration with additional distros"
   - Restart Docker Desktop

### Step 4: Verify Prerequisites (Optional but Recommended)

The installer will automatically check prerequisites before installation begins. However, you can run the checker manually first to diagnose issues before starting:

```powershell
cd C:\opentranscribe-build\opentranscribe-windows-v[VERSION]

# Run prerequisite checker (optional - installer does this automatically)
powershell -ExecutionPolicy Bypass -File .\check-prerequisites.ps1
```

This script verifies:
- Administrator privileges
- Windows version (10/11 build 17763+)
- System resources (RAM, disk, CPU)
- WSL 2 installation and configuration
- Docker Desktop status
- NVIDIA GPU availability
- Hyper-V status

**Note:** The installer will run this check automatically. This manual run is useful for:
- Pre-checking before compiling the installer
- Diagnosing issues if installation fails
- Verifying fixes after resolving problems

---

## Create Installer (Windows)

### Step 1: Install Inno Setup

1. Download Inno Setup:
   - https://jrsoftware.org/isdl.php
   - Download "Inno Setup 6.x" (latest stable)

2. Run installer:
   - Use default options
   - Install to: `C:\Program Files (x86)\Inno Setup 6`

### Step 2: Update Build Directory Path

Edit `installer.iss`:

```pascal
; Line 19: Update this path
#define BuildDir "C:\opentranscribe-build\opentranscribe-windows-v[VERSION]"
```

Change the path to match where you extracted the Windows package on your system.

### Step 3: Compile Installer

**Method 1: Inno Setup GUI**

1. Open Inno Setup Compiler
2. File → Open: Select `installer.iss`
3. Build → Compile
4. Wait for compilation (10-30 minutes for large files)
5. Installer created in: `[BuildDir]\output\OpenTranscribe-Setup-1.0.0.exe`

**Method 2: Command Line**

```powershell
cd "C:\Program Files (x86)\Inno Setup 6"
.\ISCC.exe "C:\opentranscribe-windows-build\opentranscribe-windows-v[VERSION]\installer.iss"
```

### Compilation Notes

- Uses fast compression (lzma2/fast) for installer itself
- Docker images and models use nocompression (already compressed)
- Disk spanning enabled for DVD/USB distribution
- Estimated time: 10-30 minutes depending on model size

---

## Installation

### Step 1: Run Installer

1. Double-click `OpenTranscribe-Setup-1.0.0.exe`
2. User Account Control: Click "Yes"

### Step 2: Follow Installation Wizard

1. **Welcome Screen**
   - Read pre-installation information
   - Verify prerequisites are met

2. **License Agreement**
   - Review license terms
   - Click "I accept"

3. **Installation Location**
   - Default: `C:\Program Files\OpenTranscribe`
   - Change if desired (requires 50-80GB free)

4. **Start Menu Folder**
   - Default: OpenTranscribe

5. **Additional Tasks**
   - [ ] Create desktop icon (optional)

6. **Ready to Install**
   - Review summary
   - Click "Install"

### Step 3: Automated Prerequisite Check

**IMPORTANT:** Before copying any files, the installer automatically verifies your system:

1. **Prerequisite Check Runs** (~30 seconds)
   - Checks Docker Desktop is running
   - Verifies WSL 2 configuration
   - Confirms RAM, CPU, disk space
   - Detects NVIDIA GPU (optional)

2. **If Check Fails:**
   - Installation **STOPS immediately**
   - Error dialog shows what's wrong
   - Instructions provided to fix issues
   - No files are copied (fast failure)

3. **If Check Passes:**
   - Installation proceeds automatically
   - All prerequisites confirmed
   - System ready for OpenTranscribe

**Common Failures and Fixes:**

| Error | Fix |
|-------|-----|
| Docker Desktop not running | Start Docker Desktop, wait for full startup |
| Insufficient disk space | Free up 100GB+ on C: drive |
| WSL 2 not configured | Run `wsl --install` in PowerShell (Admin) |
| RAM below 16GB | Cannot fix - hardware upgrade needed |
| Windows version too old | Update to Windows 10 build 17763+ or Windows 11 |

### Step 4: Wait for Installation

After prerequisites pass, installation takes 5-15 minutes:

1. **Copying files** (~30-60 seconds)
   - Program files, scripts, icons
   - Configuration and database schemas
   - Docker images (~10-30GB, 2-5 minutes)
   - AI models (~5-40GB, 2-10 minutes)

2. **First run initialization**
   - Generate unique secure credentials (on your machine, not shipped in the package)
   - Load Docker images into Docker Desktop
   - Configure environment variables
   - Initialize database schema
   - Start services

### Step 5: Complete Installation

1. **Installation Complete** screen
2. Review post-installation information
3. Options:
   - [x] Launch OpenTranscribe
   - Click "Finish"

---

## Post-Installation

### First Launch

The first time you run OpenTranscribe:

1. **Unique credentials are generated** (one-time, instant)
   - Secure passwords and encryption keys are generated **on your machine**
     (`generate-secrets.ps1`) — they are not shipped in the installer, so every
     installation gets its own unique secrets

2. **Docker images load** (one-time, 5-10 minutes)
   - Images are loaded from tar files
   - Tar files deleted after loading (saves space)

3. **Services start**
   - PostgreSQL database
   - MinIO storage
   - Redis cache
   - OpenSearch
   - Celery workers
   - FastAPI backend
   - Svelte frontend

4. **Browser opens** automatically
   - Navigate to: http://localhost:5173

### Initial Configuration

1. **Create admin account**
   - Navigate to http://localhost:5173
   - Register first user (becomes admin)

2. **Configure HuggingFace token** (required for speaker diarization)
   - Settings → API Keys
   - Add HuggingFace token
   - Save

3. **Configure LLM provider** (optional, for AI summarization)
   - Settings → LLM Configuration
   - Choose provider (vLLM, OpenAI, Ollama, Claude, OpenRouter)
   - Add API keys or endpoints
   - Test connection

### Verify Installation

Check all services are running:

```powershell
cd "C:\Program Files\OpenTranscribe"
docker compose -f config\docker-compose.offline.yml ps
```

Expected output:
```
NAME                    STATUS              PORTS
opentranscribe-backend  running (healthy)   0.0.0.0:5174->8080/tcp
opentranscribe-frontend running             0.0.0.0:5173->8080/tcp
opentranscribe-docs     running             0.0.0.0:3030->8080/tcp
postgres                running (healthy)   0.0.0.0:5176->5432/tcp
minio                   running (healthy)   0.0.0.0:5178-5179->9000-9001/tcp
redis                   running             0.0.0.0:5177->6379/tcp
opensearch              running (healthy)   0.0.0.0:5180-5181->9200,9600/tcp
celery-worker           running
flower                  running             0.0.0.0:5175->5555/tcp
```

### Access Points

- **Frontend**: http://localhost:5173
- **Backend API**: http://localhost:5174
- **API Documentation**: http://localhost:5174/docs
- **MinIO Console**: http://localhost:5179 (admin/adminpass)
- **Flower Task Monitor**: http://localhost:5175
- **OpenSearch Dashboards**: http://localhost:5181

---

## Troubleshooting

### Installation Issues

#### Problem: "Docker Desktop is not running"

**Solution:**
1. Start Docker Desktop from Start menu
2. Wait for Docker to fully start (icon stops animating)
3. Verify: `docker info` in PowerShell
4. Run installer again

#### Problem: "Failed to load Docker image"

**Possible causes:**
- Insufficient disk space
- Corrupted tar file during transfer
- Docker Desktop not configured properly

**Solutions:**
1. Check Docker Desktop has enough space allocated (Settings → Resources)
2. Verify file checksums:
   ```powershell
   cd docker-images
   Get-FileHash *.tar -Algorithm SHA256
   ```
3. Re-transfer files from Linux source
4. Increase Docker Desktop disk allocation

#### Problem: "Port already in use"

**Solution:**
Edit `.env` file and change conflicting ports:
```
FRONTEND_PORT=5173  → Change to 5183
BACKEND_PORT=5174   → Change to 5184
```

### Runtime Issues

#### Problem: Services not starting

**Check logs:**
```powershell
cd "C:\Program Files\OpenTranscribe"
docker compose -f config\docker-compose.offline.yml logs
```

**Common issues:**
- **PostgreSQL**: Check database initialization logs
- **MinIO**: Verify storage directory permissions
- **Backend**: Check .env configuration
- **Celery**: Verify Redis connection

#### Problem: GPU not detected

**Verify NVIDIA drivers:**
```powershell
nvidia-smi
```

**Enable GPU in Docker Desktop:**
1. Settings → Resources → WSL Integration
2. Ensure integration is enabled
3. Restart Docker Desktop

**Check container GPU access:**
```powershell
docker run --rm --gpus all nvidia/cuda:11.8-base nvidia-smi
```

#### Problem: Slow transcription (CPU mode)

If GPU is not detected, transcription uses CPU mode:
- 10-50x slower than GPU mode
- Normal behavior without NVIDIA GPU
- Consider:
  - Install NVIDIA drivers
  - Enable GPU in Docker Desktop
  - Or accept slower CPU performance

### Uninstallation Issues

#### Problem: Containers not stopping

**Manual cleanup:**
```powershell
# Stop all OpenTranscribe containers
docker ps -a --filter "name=opentranscribe" --format "{{.ID}}" | ForEach-Object { docker rm -f $_ }

# Remove volumes
docker volume ls --filter "name=opentranscribe" --format "{{.Name}}" | ForEach-Object { docker volume rm $_ }

# Remove images
docker images --filter "reference=davidamacey/opentranscribe-*" --format "{{.Repository}}:{{.Tag}}" | ForEach-Object { docker rmi $_ }
```

### Getting Help

- **GitHub Issues**: https://github.com/davidamacey/opentranscribe/issues
- **Documentation**: See README-WINDOWS.md in installation directory
- **Logs**: Check Docker logs for detailed error messages

---

## Additional Resources

### Useful Commands

**View all logs:**
```powershell
docker compose -f config\docker-compose.offline.yml logs -f
```

**View specific service logs:**
```powershell
docker compose -f config\docker-compose.offline.yml logs -f backend
docker compose -f config\docker-compose.offline.yml logs -f celery-worker
```

**Restart services:**
```powershell
docker compose -f config\docker-compose.offline.yml restart
```

**Stop services:**
```powershell
docker compose -f config\docker-compose.offline.yml down
```

**Start services:**
```powershell
docker compose -f config\docker-compose.offline.yml up -d
```

**Check service status:**
```powershell
docker compose -f config\docker-compose.offline.yml ps
```

### File Locations

- **Installation**: `C:\Program Files\OpenTranscribe`
- **Models**: `C:\Program Files\OpenTranscribe\models`
- **Config**: `C:\Program Files\OpenTranscribe\config`
- **Database**: Docker volume `postgres_data`
- **Storage**: Docker volume `minio_data`
- **Logs**: Docker container logs (use `docker logs`)

### Performance Tuning

**Increase Docker resources:**
1. Docker Desktop → Settings → Resources
2. Memory: 16GB+ (32GB for optimal performance)
3. CPUs: 4-8 cores
4. Disk: 200GB+
5. Swap: 8GB

**Configure WSL 2:**
Edit `%USERPROFILE%\.wslconfig`:
```ini
[wsl2]
memory=32GB
processors=8
swap=8GB
localhostForwarding=true
```

Restart WSL 2:
```powershell
wsl --shutdown
```

---

## Maintenance and Updates

### Updating Installer Files

All installer files are in the `windows-installer/` directory. To update:

1. **Edit source files** in `windows-installer/` directory
2. **Rebuild package** using `build-windows-installer.sh` on Linux
3. **Transfer** to Windows and recompile with Inno Setup

Files automatically updated during build:
- `.env` is generated from `.env.example`
- `docker-compose.offline.yml` gets image versions from `docker-compose.yml`
- `database/init_db.sql` is copied from latest
- All `windows-installer/` files are copied as-is

### Version Updates

To update version number, edit `installer.iss` line ~8:
```pascal
#define MyAppVersion "1.1.0"  // Change this
```

Then rebuild installer. Output will be: `OpenTranscribe-Setup-1.1.0.exe`

### Updating AI Models

To change the Whisper model included:
```bash
# Set before building
export WHISPER_MODEL="large-v3-turbo"  # default; or large-v3, large-v2, medium, small, base, tiny
./scripts/build-windows-installer.sh
```

Model selection affects:
- Package size (5-40GB difference)
- Transcription quality
- GPU memory requirements
- Processing speed

### Development Notes

**Inno Setup Best Practices:**
- Use `nocompression` for Docker images and AI models (already compressed)
- Use `lzma2/fast` compression for scripts and configs
- Enable disk spanning for installers >4GB
- Require admin privileges for Docker operations
- Include comprehensive error messages and troubleshooting

**Batch Script Best Practices:**
- Use `setlocal EnableDelayedExpansion` for variable handling
- Support `/SILENT` mode for automated installs
- Provide verbose output in interactive mode
- Return proper exit codes (0=success, 1=failure)
- Test both interactive and silent modes thoroughly

**PowerShell Script Best Practices:**
- Use `#Requires -Version 5.1` for compatibility
- Support `-Silent` switch parameter
- Provide color-coded output (Green=success, Yellow=warning, Red=error)
- Return meaningful exit codes
- Gracefully handle non-admin execution

---

## Support and Resources

### Documentation

- **User Documentation**: [README-WINDOWS.md](README-WINDOWS.md) - Included in installer for end users
- **This Guide**: Complete build and installation process
- **Main Project**: [../../README.md](../../README.md) - Full OpenTranscribe documentation

### Getting Help

- **Build Issues**: Check build script output and logs
- **Transfer Issues**: Verify file integrity using checksums
- **Compilation Issues**: Review Inno Setup compiler messages
- **Installation Issues**: Run `check-prerequisites.ps1` first
- **Runtime Issues**: Check Docker container logs

### Links

- **GitHub Repository**: https://github.com/davidamacey/opentranscribe
- **Issue Tracker**: https://github.com/davidamacey/opentranscribe/issues
- **Docker Desktop**: https://www.docker.com/products/docker-desktop
- **Inno Setup**: https://jrsoftware.org/isdl.php
- **HuggingFace**: https://huggingface.co/settings/tokens

---

## License

See [license.txt](license.txt) for full license terms and third-party component licenses.
