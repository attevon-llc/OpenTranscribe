# OpenTranscribe Windows Installer

## Overview

This package contains everything needed to install OpenTranscribe on a Windows system with Docker Desktop.

## Prerequisites

Before installation, ensure you have:

1. **Windows 10/11 Pro, Enterprise, or Education** (Home edition requires extra WSL2 setup)
2. **Docker Desktop for Windows** - Installed and running
3. **WSL 2** - Enabled (required by Docker Desktop)
4. **NVIDIA GPU** (Optional but recommended)
   - NVIDIA drivers installed (version 450.80.02 or higher)
   - CUDA support enabled in Docker Desktop settings
5. **Minimum System Requirements:**
   - 16GB RAM (32GB recommended)
   - 100GB free disk space
   - 4 CPU cores (8 cores recommended)

## Installation Instructions

### Step 1: Extract Package on Linux

On your Linux build system, run:
```bash
./scripts/build-windows-installer.sh
```

This creates: `windows-installer-build/opentranscribe-windows-v[VERSION]/`

### Step 2: Transfer to Windows

Transfer the entire folder to your Windows machine using:
- USB drive (formatted as exFAT or NTFS, NOT FAT32)
- Network share
- Cloud storage

### Step 3: Verify Prerequisites on Windows

Open PowerShell as Administrator and run:
```powershell
.\check-prerequisites.ps1
```

This script checks:
- Docker Desktop installation and status
- WSL 2 configuration
- NVIDIA driver availability
- Disk space
- Memory

If any prerequisites are missing, follow the on-screen instructions to install them.

### Step 4: Run Inno Setup Compiler

1. Install Inno Setup if not already installed: https://jrsoftware.org/isdl.php
2. Open `installer.iss` in Inno Setup Compiler
3. Click **Build** → **Compile**
4. The installer executable will be created in the output directory

### Step 5: Run the Installer

1. Double-click the generated `.exe` file
2. Follow the installation wizard
3. **Automatic prerequisite check** runs before installation:
   - Verifies Docker Desktop is running
   - Checks WSL 2 configuration
   - Confirms system resources (RAM, disk, CPU)
   - Detects GPU availability
   - **Installation STOPS if prerequisites fail** (with detailed fix instructions)
4. If prerequisites pass, the installer will:
   - Load Docker images
   - Configure environment variables
   - Start OpenTranscribe services
   - Open the application in your browser

## Post-Installation

After installation, access OpenTranscribe at:
- **Frontend:** http://localhost:5173
- **Backend API:** http://localhost:5174
- **API Documentation:** http://localhost:5174/docs
- **MinIO Console:** http://localhost:5179
- **Flower (Task Monitor):** http://localhost:5175

## Model Cache

AI models are stored in the installation directory under `models/`:
- `models/huggingface/` - WhisperX transcription models
- `models/torch/` - PyAnnote diarization models (the only diarization engine this
  installer ships — the native `diar-native` sidecar is not packaged for Windows,
  see INSTALL-WINDOWS.md's HuggingFace token step)
- `models/nltk_data/` - Natural language processing data
- `models/sentence-transformers/` - Semantic search models

Total size: ~5-40GB depending on Whisper model size selected during build.

## Troubleshooting

### Docker Images Not Loading
- Ensure Docker Desktop is running
- Check available disk space (need 40GB+ free)
- Verify files were not corrupted during transfer (check checksums)

### Services Not Starting
- Check Docker Desktop is in Linux containers mode (not Windows containers)
- Verify WSL 2 is properly configured: `wsl --list --verbose`
- Check logs: `docker compose -f config\docker-compose.yml -f config\docker-compose.offline.yml logs`

### GPU Not Detected
- Verify NVIDIA drivers are installed: run `nvidia-smi` in PowerShell
- Enable CUDA support in Docker Desktop: Settings → Resources → WSL Integration
- Restart Docker Desktop after enabling GPU support

### Port Conflicts
If default ports are in use, you can change them in the `.env` file before running the installer.

## Uninstallation

To uninstall OpenTranscribe:
1. Run `uninstall_opentranscribe.bat` from the installation directory
2. Or use Windows "Add or Remove Programs"

This will:
- Stop all Docker containers
- Remove Docker images and volumes
- Delete configuration files
- Optionally remove data directories (you will be prompted)

## Offline Operation

This installer is designed for **completely offline operation**:
- All Docker images are included (no internet required)
- All AI models are pre-downloaded
- No external API calls during installation or operation

## Support

For issues, questions, or contributions:
- GitHub: https://github.com/yourusername/opentranscribe
- Documentation: See main README.md
