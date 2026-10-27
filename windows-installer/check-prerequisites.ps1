# OpenTranscribe Prerequisites Checker
# Verifies that all required components are installed before OpenTranscribe installation
# Run this script as Administrator before installing OpenTranscribe

#Requires -Version 5.1

param(
    [switch]$Silent = $false
)

# Set strict mode and error handling
$ErrorActionPreference = "Continue"
$WarningPreference = "Continue"

# Color output functions
function Write-ColorOutput {
    param(
        [string]$Message,
        [string]$Color = "White"
    )

    if (-not $Silent) {
        Write-Host $Message -ForegroundColor $Color
    }
}

function Write-Success {
    param([string]$Message)
    Write-ColorOutput "✓ $Message" "Green"
}

function Write-Failure {
    param([string]$Message)
    Write-ColorOutput "✗ $Message" "Red"
}

function Write-Warning {
    param([string]$Message)
    Write-ColorOutput "⚠ $Message" "Yellow"
}

function Write-Info {
    param([string]$Message)
    Write-ColorOutput "ℹ $Message" "Cyan"
}

function Write-Header {
    param([string]$Message)
    Write-ColorOutput "`n========================================" "Cyan"
    Write-ColorOutput "  $Message" "Cyan"
    Write-ColorOutput "========================================`n" "Cyan"
}

# Track overall status
$script:AllChecksPassed = $true
$script:Warnings = @()
$script:Errors = @()

function Add-Error {
    param([string]$Message)
    $script:Errors += $Message
    $script:AllChecksPassed = $false
}

function Add-Warning {
    param([string]$Message)
    $script:Warnings += $Message
}

#######################
# CHECK FUNCTIONS
#######################

function Test-Administrator {
    Write-Header "Checking Administrator Privileges"

    $currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if ($isAdmin) {
        Write-Success "Running with Administrator privileges"
        return $true
    } else {
        Write-Failure "Not running as Administrator"
        Add-Error "This script must be run as Administrator. Right-click and select 'Run as Administrator'"
        return $false
    }
}

function Test-WindowsVersion {
    Write-Header "Checking Windows Version"

    $os = Get-CimInstance Win32_OperatingSystem
    $version = [System.Version]$os.Version
    $minVersion = [System.Version]"10.0.17763"  # Windows 10 1809

    Write-Info "OS: $($os.Caption)"
    Write-Info "Version: $($os.Version) (Build $($os.BuildNumber))"
    Write-Info "Edition: $($os.OperatingSystemSKU)"

    if ($version -ge $minVersion) {
        Write-Success "Windows version is compatible (10.0.17763 or higher)"

        # Check edition
        $edition = $os.Caption
        if ($edition -match "Pro|Enterprise|Education") {
            Write-Success "Windows edition is recommended: $edition"
        } elseif ($edition -match "Home") {
            Write-Warning "Windows Home edition detected. Docker Desktop requires additional WSL 2 configuration."
            Add-Warning "Windows Home requires manual WSL 2 setup for Docker Desktop"
        }

        return $true
    } else {
        Write-Failure "Windows version is too old (need 10.0.17763 or higher)"
        Add-Error "Please upgrade to Windows 10 version 1809 (October 2018 Update) or later"
        return $false
    }
}

function Test-SystemResources {
    Write-Header "Checking System Resources"

    $allGood = $true

    # Check RAM
    $ram = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB
    Write-Info "Total RAM: $([math]::Round($ram, 2)) GB"

    if ($ram -ge 32) {
        Write-Success "Excellent RAM for OpenTranscribe ($([math]::Round($ram, 2)) GB)"
    } elseif ($ram -ge 16) {
        Write-Success "Sufficient RAM for OpenTranscribe ($([math]::Round($ram, 2)) GB)"
    } else {
        Write-Failure "Insufficient RAM ($([math]::Round($ram, 2)) GB, need 16GB minimum)"
        Add-Error "OpenTranscribe requires at least 16GB RAM. Current: $([math]::Round($ram, 2)) GB"
        $allGood = $false
    }

    # Check CPU cores
    $cpuCores = (Get-CimInstance Win32_Processor).NumberOfLogicalProcessors
    Write-Info "CPU Cores: $cpuCores"

    if ($cpuCores -ge 8) {
        Write-Success "Excellent CPU configuration ($cpuCores cores)"
    } elseif ($cpuCores -ge 4) {
        Write-Success "Sufficient CPU configuration ($cpuCores cores)"
    } else {
        Write-Warning "Limited CPU cores ($cpuCores cores, 4+ recommended)"
        Add-Warning "Performance may be limited with fewer than 4 CPU cores"
    }

    # Check disk space on C: drive (100GB for installed system + runtime)
    # Note: Linux build-offline-package.sh requires 80GB for the build process only (temp files cleaned after)
    $disk = Get-CimInstance Win32_LogicalDisk | Where-Object {$_.DeviceID -eq "C:"}
    $freeSpaceGB = [math]::Round($disk.FreeSpace / 1GB, 2)
    $totalSpaceGB = [math]::Round($disk.Size / 1GB, 2)

    Write-Info "C: Drive - Free: $freeSpaceGB GB / Total: $totalSpaceGB GB"

    if ($freeSpaceGB -ge 200) {
        Write-Success "Excellent disk space ($freeSpaceGB GB free)"
    } elseif ($freeSpaceGB -ge 100) {
        Write-Success "Sufficient disk space ($freeSpaceGB GB free)"
    } elseif ($freeSpaceGB -ge 50) {
        Write-Warning "Limited disk space ($freeSpaceGB GB free, 100GB+ recommended)"
        Add-Warning "Low disk space may cause installation or runtime issues"
    } else {
        Write-Failure "Insufficient disk space ($freeSpaceGB GB free, need 100GB minimum)"
        Add-Error "Free up disk space before installing. Need at least 100GB free."
        $allGood = $false
    }

    return $allGood
}

function Test-WSL2 {
    Write-Header "Checking WSL 2 (Windows Subsystem for Linux)"

    # Check if WSL is installed
    $wslFeature = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -ErrorAction SilentlyContinue

    if ($null -eq $wslFeature) {
        Write-Failure "WSL is not installed"
        Add-Error "WSL 2 is required for Docker Desktop. Install with: wsl --install"
        return $false
    }

    if ($wslFeature.State -eq "Enabled") {
        Write-Success "WSL is installed and enabled"
    } else {
        Write-Failure "WSL is installed but not enabled"
        Add-Error "Enable WSL with: dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart"
        return $false
    }

    # Check if VirtualMachinePlatform is enabled (required for WSL 2)
    $vmFeature = Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -ErrorAction SilentlyContinue

    if ($null -eq $vmFeature) {
        Write-Warning "Virtual Machine Platform feature not found"
        Add-Warning "WSL 2 requires Virtual Machine Platform. Enable with: dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart"
        return $true  # Don't fail, Docker installer may handle this
    }

    if ($vmFeature.State -eq "Enabled") {
        Write-Success "Virtual Machine Platform is enabled (required for WSL 2)"
    } else {
        Write-Warning "Virtual Machine Platform is not enabled"
        Add-Warning "Enable with: dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart"
    }

    # Check WSL version
    try {
        $wslOutput = wsl --list --verbose 2>&1 | Out-String
        if ($wslOutput -match "VERSION\s+2") {
            Write-Success "WSL 2 is configured"
        } else {
            Write-Warning "WSL 2 may not be set as default. Set with: wsl --set-default-version 2"
            Add-Warning "WSL 2 should be the default version for Docker Desktop"
        }
    } catch {
        Write-Info "Unable to check WSL version (this is OK if Docker Desktop will configure it)"
    }

    return $true
}

function Test-DockerDesktop {
    Write-Header "Checking Docker Desktop"

    # Check if Docker Desktop is installed
    $dockerPath = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    $dockerExists = Test-Path $dockerPath

    if (-not $dockerExists) {
        Write-Failure "Docker Desktop is not installed"
        Add-Error "Docker Desktop is required. Download from: https://www.docker.com/products/docker-desktop"
        return $false
    }

    Write-Success "Docker Desktop is installed"

    # Check if Docker is running
    try {
        $dockerInfo = docker info 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Success "Docker Desktop is running"

            # Get Docker version
            $dockerVersion = docker version --format '{{.Server.Version}}' 2>$null
            Write-Info "Docker version: $dockerVersion"

            # Check if running in Linux containers mode
            if ($dockerInfo -match "OSType: linux") {
                Write-Success "Docker is in Linux containers mode (required)"
            } else {
                Write-Failure "Docker is not in Linux containers mode"
                Add-Error "Switch Docker to Linux containers mode (right-click Docker icon → Switch to Linux containers)"
                return $false
            }

            return $true
        } else {
            Write-Failure "Docker Desktop is installed but not running"
            Add-Error "Start Docker Desktop before installing OpenTranscribe"
            return $false
        }
    } catch {
        Write-Failure "Docker command not available"
        Add-Error "Docker Desktop is installed but not properly configured. Try restarting Docker Desktop."
        return $false
    }
}

function Test-NvidiaGPU {
    Write-Header "Checking NVIDIA GPU (Optional)"

    # Check for nvidia-smi
    $nvidiaSmi = "C:\Windows\System32\nvidia-smi.exe"
    $nvidiaExists = Test-Path $nvidiaSmi

    if (-not $nvidiaExists) {
        Write-Info "No NVIDIA GPU detected (nvidia-smi.exe not found)"
        Write-Info "OpenTranscribe will run in CPU mode (slower but functional)"
        Add-Warning "GPU acceleration not available. Transcription will be slower."
        return $true  # Not a failure, just a warning
    }

    try {
        $gpuInfo = & $nvidiaSmi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1

        if ($LASTEXITCODE -eq 0) {
            Write-Success "NVIDIA GPU detected!"

            # Parse GPU info
            $gpuData = $gpuInfo -split ","
            $gpuName = $gpuData[0].Trim()
            $driverVersion = $gpuData[1].Trim()
            $gpuMemory = $gpuData[2].Trim()

            Write-Info "GPU: $gpuName"
            Write-Info "Driver Version: $driverVersion"
            Write-Info "GPU Memory: $gpuMemory"

            # Check driver version (need 527.41 or higher (CUDA 12))
            $driverVersionNum = [version]($driverVersion -replace '[^0-9.]', '')
            $minDriverVersion = [version]"527.41"

            if ($driverVersionNum -ge $minDriverVersion) {
                Write-Success "NVIDIA driver version is compatible ($driverVersion)"
            } else {
                Write-Warning "NVIDIA driver version is old ($driverVersion, need 527.41+)"
                Add-Warning "Update NVIDIA drivers for GPU acceleration: https://www.nvidia.com/Download/index.aspx"
            }

            # Check GPU memory (8GB+ recommended)
            if ($gpuMemory -match "(\d+)") {
                $memoryGB = [int]$matches[1] / 1024
                if ($memoryGB -ge 8) {
                    Write-Success "GPU has sufficient memory for optimal performance ($([math]::Round($memoryGB, 1))GB)"
                } elseif ($memoryGB -ge 4) {
                    Write-Info "GPU memory is adequate ($([math]::Round($memoryGB, 1))GB, 8GB+ recommended for large models)"
                } else {
                    Write-Warning "GPU memory is limited ($([math]::Round($memoryGB, 1))GB, may affect performance)"
                }
            }

            return $true
        } else {
            Write-Warning "NVIDIA GPU detected but nvidia-smi failed to run"
            Add-Warning "Check NVIDIA driver installation"
            return $true
        }
    } catch {
        Write-Warning "Error checking NVIDIA GPU: $_"
        return $true
    }
}

function Test-HyperV {
    Write-Header "Checking Hyper-V (Optional but recommended)"

    try {
        $hypervFeature = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -ErrorAction SilentlyContinue

        if ($null -eq $hypervFeature) {
            Write-Info "Hyper-V feature not found (may not be available on this Windows edition)"
            Write-Info "WSL 2 will work without Hyper-V on Windows Home"
            return $true
        }

        if ($hypervFeature.State -eq "Enabled") {
            Write-Success "Hyper-V is enabled (optimal for Docker performance)"
        } else {
            Write-Info "Hyper-V is not enabled (OK for WSL 2 backend)"
            Write-Info "Enable for best performance: Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All"
        }

        return $true
    } catch {
        Write-Info "Unable to check Hyper-V status (this is OK)"
        return $true
    }
}

#######################
# MAIN EXECUTION
#######################

Clear-Host

Write-ColorOutput @"
╔════════════════════════════════════════════════════════════╗
║                                                            ║
║        OpenTranscribe Prerequisites Checker v1.0          ║
║                                                            ║
║     Verifying system requirements for installation        ║
║                                                            ║
╚════════════════════════════════════════════════════════════╝
"@ "Cyan"

# Run all checks
Test-Administrator
Test-WindowsVersion
Test-SystemResources
Test-WSL2
Test-DockerDesktop
Test-NvidiaGPU
Test-HyperV

# Summary
Write-Header "Prerequisites Check Summary"

if ($script:Errors.Count -eq 0 -and $script:Warnings.Count -eq 0) {
    Write-ColorOutput "`n✓ All prerequisites met! You can proceed with OpenTranscribe installation.`n" "Green"
} elseif ($script:Errors.Count -eq 0) {
    Write-ColorOutput "`n⚠ Prerequisites met with warnings:`n" "Yellow"
    foreach ($warning in $script:Warnings) {
        Write-ColorOutput "  • $warning" "Yellow"
    }
    Write-ColorOutput "`nYou can proceed with installation, but performance may be affected.`n" "Yellow"
} else {
    Write-ColorOutput "`n✗ Prerequisites check failed. Please resolve the following issues:`n" "Red"
    foreach ($error in $script:Errors) {
        Write-ColorOutput "  • $error" "Red"
    }

    if ($script:Warnings.Count -gt 0) {
        Write-ColorOutput "`nAdditional warnings:`n" "Yellow"
        foreach ($warning in $script:Warnings) {
            Write-ColorOutput "  • $warning" "Yellow"
        }
    }

    Write-ColorOutput "`nInstallation cannot proceed until these issues are resolved.`n" "Red"
}

# Exit with appropriate code
if ($script:AllChecksPassed) {
    exit 0
} else {
    exit 1
}
