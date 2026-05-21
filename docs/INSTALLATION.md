# OpenTranscribe One-Line Installation Guide
<!-- Updated for v0.4.0 -->

## 🚀 Quick Installation (Any Platform)

**Copy and paste this single command on any operating system:**

```bash
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

**That's it!** The script will:
- ✅ Detect your hardware automatically (NVIDIA GPU, Apple Silicon, CPU)
- ✅ Configure optimal settings for your platform
- ✅ Set up Docker with proper runtime
- ✅ Download all necessary files
- ✅ Create management scripts
- ✅ Generate secure configuration

## 🌍 Platform Compatibility

### ✅ **Linux (All Distributions)**
```bash
# Ubuntu, Debian, CentOS, Fedora, Arch, etc.
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

### ✅ **macOS (Intel & Apple Silicon)**
```bash
# Works on both Intel Macs and Apple Silicon (M1/M2)
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

### ✅ **Windows (WSL2)**
```bash
# In WSL2 terminal
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

### ✅ **Windows (Git Bash/Cygwin)**
```bash
# In Git Bash or Cygwin terminal
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

## 🧮 CPU-Only Installation (Opt-Out of GPU)

Auto-detection enables GPU acceleration whenever it finds a working NVIDIA
setup. If you **know** you want to run on CPU only — either because you have
no NVIDIA GPU, or because the NVIDIA Container Toolkit is installed but GPU
passthrough is not functional (a common situation on WSL2 without a
WSL-capable Windows driver) — pass `--cpu` to skip GPU detection entirely:

```bash
# Piped install: forward --cpu with `bash -s --`
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash -s -- --cpu

# Or via env var (useful in CI / unattended installs)
OPENTRANSCRIBE_FORCE_CPU=1 curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash

# Or after downloading the script
./setup-opentranscribe.sh --cpu
```

When `--cpu` is active the installer:

- Skips `nvidia-smi` and NVIDIA Container Toolkit probing.
- Writes `FORCE_CPU_MODE=true`, `DETECTED_DEVICE=cpu`, and CPU-optimized
  precision (`int8`) / batch-size defaults into `.env`.
- Causes `opentranscribe.sh start` (and subsequent `restart`/`stop` calls) to
  skip the `docker-compose.gpu.yml` / `docker-compose.blackwell.yml`
  overlays. You do **not** need to re-pass `--cpu` on every run.

To switch a CPU-only install back to GPU later, re-run the installer without
`--cpu` (or set `FORCE_CPU_MODE=false` in `.env` and restart).

## 📋 Prerequisites

**Required (automatically checked):**
- Docker (with daemon running)
- Docker Compose v2+
- `curl` command
- Internet connection

**Optional (auto-detected):**
- NVIDIA GPU + NVIDIA Container Toolkit (for GPU acceleration)
- HuggingFace account (for speaker diarization)

## 🔧 What Happens During Installation

1. **Platform Detection**: Identifies your OS and architecture
2. **Hardware Detection**: Scans for NVIDIA GPU, Apple Silicon, or CPU
3. **Docker Validation**: Checks Docker installation and runtime capabilities
4. **Configuration Download**: Downloads optimized docker-compose and config files
5. **Environment Setup**: Creates secure `.env` with hardware-specific settings
6. **Script Creation**: Generates management script for daily operations
7. **Validation**: Confirms everything is properly configured

## 🎯 Hardware-Specific Optimizations

| Platform | Device | Precision | Batch Size | Recommended Model |
|----------|--------|-----------|------------|-------------------|
| Linux + NVIDIA RTX 40x0 | `cuda` | `float16` | `16` | `large-v3-turbo` |
| Linux + NVIDIA RTX 30x0 | `cuda` | `float16` | `8-12` | `large-v3-turbo` |
| Linux + NVIDIA Blackwell (GB10x/GB20x) | `cuda` | `float16` | `16` | `large-v3-turbo` |
| Linux + NVIDIA GTX/older | `cuda` | `float32` | `4-8` | `medium` |
| macOS + M2 Max/Ultra | `mps` | `float32` | `8` | `large-v3-turbo` |
| macOS + M1/M2 | `mps` | `float32` | `4-6` | `medium` |
| Intel Mac | `cpu` | `int8` | `2-4` | `base` |
| Any CPU (16+ cores) | `cpu` | `int8` | `4` | `small` |
| Any CPU (8+ cores) | `cpu` | `int8` | `2` | `base` |
| Any CPU (4 cores) | `cpu` | `int8` | `1` | `base` |

> **Note:** `large-v3-turbo` is the default model as of v0.4.0 — approximately 6x faster than `large-v2` for English and most languages. Use `large-v3` when translation to English or maximum multilingual accuracy is required (turbo was not trained for translation tasks).

## 🚀 After Installation

**Start OpenTranscribe:**
```bash
cd opentranscribe
./opentranscribe.sh start
```

**Access the application:**
- Web Interface: http://localhost:5173
- API Documentation: http://localhost:8080/docs
- Task Monitor: http://localhost:5555/flower

## 🔐 Authentication Configuration

OpenTranscribe supports multiple authentication methods for different deployment scenarios:

### Authentication Methods

| Method | Use Case | Documentation |
|--------|----------|---------------|
| **Local** | Default, standalone deployments | Built-in (bcrypt password hashing) |
| **LDAP/AD** | Enterprise with Active Directory | [LDAP_AUTH.md](LDAP_AUTH.md) |
| **OIDC/Keycloak** | SSO, OAuth 2.0 with PKCE | [KEYCLOAK_SETUP.md](KEYCLOAK_SETUP.md) |
| **PKI/X.509** | Government, CAC/PIV smart cards | [PKI_SETUP.md](PKI_SETUP.md) |

### Quick Configuration

**Local Authentication (Default):**
```bash
# No additional configuration required
# Users register via /api/auth/register or are created by admins
```

**LDAP/Active Directory:**
```bash
# Add to .env
LDAP_ENABLED=true
LDAP_SERVER=ldaps://your-ad-server.domain.com
LDAP_PORT=636
LDAP_BIND_DN=CN=service-account,CN=Users,DC=domain,DC=com
LDAP_BIND_PASSWORD=your-service-account-password
LDAP_SEARCH_BASE=DC=domain,DC=com
```

**OIDC/Keycloak:**
```bash
# Add to .env
OIDC_ENABLED=true
OIDC_DISCOVERY_URL=https://keycloak.example.com/realms/opentranscribe/.well-known/openid-configuration
OIDC_CLIENT_ID=opentranscribe
OIDC_CLIENT_SECRET=your-client-secret
```

**PKI/X.509 Certificates:**
```bash
# Add to .env
PKI_ENABLED=true
PKI_CA_CERT_PATH=/path/to/ca-certificates.pem
PKI_HEADER_NAME=X-SSL-Client-Cert
```

### Security Features

All authentication methods support:
- **MFA/TOTP**: Two-factor authentication with backup codes
- **Password Policies**: Configurable complexity, history, and expiration
- **Account Lockout**: Automatic lockout after failed attempts
- **Session Management**: Token rotation and secure session handling
- **Audit Logging**: Comprehensive logging of all auth events

See [SECURITY.md](SECURITY.md) for detailed security configuration and [TESTING_CHECKLIST.md](TESTING_CHECKLIST.md) for verification procedures.

## GPU-Free (Lite) Deployment

v0.4.0 introduces `DEPLOYMENT_MODE=lite` for environments without a local GPU. In lite mode all AI transcription is handled by cloud ASR providers (Deepgram, AssemblyAI, OpenAI Whisper API, Google, Azure, AWS, Speechmatics, Gladia) — no WhisperX or PyAnnote containers are started.

```bash
# Add to .env
DEPLOYMENT_MODE=lite

# Configure at least one cloud ASR provider, e.g.:
DEEPGRAM_API_KEY=your-key
```

Lite mode is also useful for development and CI environments where a GPU is not available. Full local GPU mode remains the default (`DEPLOYMENT_MODE=full`).

## 🔒 HTTPS/SSL Setup (For Network Access)

**Why HTTPS?** Browser microphone recording requires HTTPS when accessing from devices other than localhost. This is a browser security requirement, not an OpenTranscribe limitation.

### Option 1: Configure During Installation

During the one-line installation, you'll be prompted:
```
Do you want to set up HTTPS with self-signed certificates? (y/N)
```

If you answer **yes**, the installer will:
- Ask for a hostname (e.g., `opentranscribe.local`)
- Generate SSL certificates automatically
- Configure NGINX reverse proxy
- Update your `.env` file

### Option 2: Configure After Installation

```bash
cd opentranscribe
./opentranscribe.sh setup-ssl
```

This interactive command will:
1. Prompt for your hostname
2. Generate SSL certificates
3. Update your configuration
4. Show next steps for DNS setup

### DNS Configuration

After SSL setup, add your hostname to DNS:

**Option A: Router DNS**
Add a DNS entry pointing your hostname to your server's IP.

**Option B: Local hosts file**
Add to `/etc/hosts` (Linux/Mac) or `C:\Windows\System32\drivers\etc\hosts` (Windows):
```
192.168.1.100  opentranscribe.local
```

### Trust the Certificate

Self-signed certificates require trusting on each client device:
- **Copy** `nginx/ssl/server.crt` to client devices
- **Import** into browser/system certificate store

See [NGINX_SETUP.md](docs/NGINX_SETUP.md) for detailed platform-specific instructions.

### Access via HTTPS

After setup, access at: `https://your-hostname`

All services are available through the NGINX reverse proxy:
- Web Interface: `https://your-hostname`
- API: `https://your-hostname/api`
- Flower: `https://your-hostname/flower/`
- MinIO: `https://your-hostname/minio/`

## 🛠️ Edge Cases & Troubleshooting

### **Issue: Permission Denied**
```bash
# If you get permission denied, run with explicit bash:
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh -o setup.sh
chmod +x setup.sh
./setup.sh
```

### **Issue: No curl Command**
```bash
# On older systems without curl, use wget:
wget -qO- https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash

# Or download manually:
wget https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh
bash setup-opentranscribe.sh
```

### **Issue: Corporate Firewall/Proxy**
```bash
# Set proxy environment variables:
export http_proxy=http://proxy.company.com:8080
export https_proxy=http://proxy.company.com:8080
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

### **Issue: Docker Not Running**
```bash
# Start Docker daemon:
sudo systemctl start docker      # Linux (systemd)
sudo service docker start       # Linux (service)
open -a Docker                  # macOS
# Or start Docker Desktop manually
```

### **Issue: NVIDIA GPU Not Detected**
```bash
# Check GPU availability:
nvidia-smi

# Install NVIDIA Container Toolkit:
# Ubuntu/Debian:
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart docker

# Test GPU in Docker:
docker run --rm --gpus all nvidia/cuda:11.8-base-ubuntu22.04 nvidia-smi
```

### **Issue: Apple Silicon Docker Issues**
```bash
# Ensure Docker Desktop is set to use Apple Silicon emulation:
# Docker Desktop > Settings > General > "Use Rosetta for x86/amd64 emulation" (disabled)
# Docker Desktop > Settings > Features in Development > "Use containerd for pulling and storing images" (enabled)
```

### **Issue: Low Memory/Storage**
```bash
# Check available space (needs ~10GB):
df -h

# Check available memory (needs ~4GB):
free -h  # Linux
vm_stat  # macOS

# Clean Docker if needed:
docker system prune -a
```

### **Issue: Windows Subsystem for Linux (WSL2)**
```bash
# Ensure WSL2 is properly configured:
wsl --list --verbose  # Should show version 2
wsl --set-default-version 2

# Install WSL2 Docker integration:
# Install Docker Desktop for Windows with WSL2 backend
```

### **Issue: Slow Internet/Timeouts**
```bash
# Increase timeout and retry:
curl -fsSL --max-time 300 --retry 3 https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

### **Issue: Different Architecture**
```bash
# For ARM64 systems (Raspberry Pi, etc.):
export TARGETPLATFORM=linux/arm64
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

## 🔄 Manual Installation (Alternative)

If the one-line installation doesn't work, you can install manually:

```bash
# 1. Create directory and download
mkdir opentranscribe && cd opentranscribe
curl -O https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh
chmod +x setup-opentranscribe.sh

# 2. Run setup
./setup-opentranscribe.sh

# 3. Start application
./opentranscribe.sh start
```

## 📞 Support

If you encounter any issues:

1. **Check Prerequisites**: Ensure Docker is installed and running
2. **Review Logs**: Look at the setup script output for error messages
3. **Hardware Validation**: Run `docker run --rm hello-world` to test Docker
4. **GPU Testing**: Run `nvidia-smi` (NVIDIA) or check system info (Apple Silicon)
5. **Create Issue**: Open a GitHub issue with your platform details and error logs

## 🔐 Security Notes

- The setup script downloads files from GitHub using HTTPS
- JWT secrets are automatically generated using secure random methods
- No sensitive data is transmitted during installation
- All containers run with non-root users where possible

## 🌟 Advanced Usage

**Custom Installation Directory:**
```bash
export PROJECT_DIR="my-transcribe-app"
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

**Force Specific Hardware:**
```bash
export TORCH_DEVICE=cpu  # Force CPU mode
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

**Skip Interactive Prompts:**
```bash
export HUGGINGFACE_TOKEN=your_token_here
export WHISPER_MODEL=base
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

## 🔄 Updating OpenTranscribe

### Check for Updates
```bash
./opentranscribe.sh version
```

### Update Docker Containers Only
```bash
./opentranscribe.sh update
```
This pulls the latest Docker images and restarts services. Your configuration and data are preserved.

### Full Update (Recommended)
```bash
./opentranscribe.sh update-full
```
This updates:
- Docker container images
- Configuration files (docker-compose, nginx)
- Management scripts
- Helper scripts

Your `.env` configuration, SSL certificates, database, and transcriptions are preserved.

---

**Ready to transcribe on any platform with one command! 🎉**
