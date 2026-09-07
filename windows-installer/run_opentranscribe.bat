@echo off
REM ============================================================================
REM OpenTranscribe Windows Startup Script
REM ============================================================================
REM
REM Purpose: Start OpenTranscribe AI transcription application on Windows
REM
REM This script performs the following:
REM 1. Verifies Docker Desktop is running
REM 2. Detects NVIDIA GPU availability
REM 3. Loads Docker images from tar files (first run only)
REM 4. Starts OpenTranscribe services using docker compose
REM 5. Opens the application in the default browser
REM
REM Usage: Double-click run_opentranscribe.bat or run from command prompt
REM
REM Access OpenTranscribe after startup: http://localhost:5173
REM
REM Prerequisites:
REM - Docker Desktop installed and running
REM - WSL 2 configured
REM - NVIDIA GPU drivers (optional, for GPU acceleration)
REM ============================================================================

setlocal EnableDelayedExpansion

echo ========================================
echo    OpenTranscribe Startup
echo ========================================
echo.

REM Get the installation directory (where this script is located)
set "INSTALL_DIR=%~dp0"
cd /d "%INSTALL_DIR%"

REM Check if Docker Desktop is running
echo Checking Docker Desktop status...
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Docker Desktop is not running!
    echo.
    echo Please start Docker Desktop and try again.
    echo 1. Click the Docker icon in your system tray
    echo 2. If Docker is not running, start it from the Start menu
    echo 3. Wait for Docker to be fully started (icon stops animating)
    echo 4. Run this script again
    echo.
    pause
    exit /b 1
)

echo Docker Desktop is running.
echo.

REM ============================================================================
REM First run: generate secure credentials on THIS machine.
REM Secrets are not baked into the installer package - every installation
REM gets its own unique passwords and encryption keys.
REM ============================================================================
findstr /c:"CHANGE_ME_auto_generated_on_install" "%INSTALL_DIR%.env" >nul 2>&1
if %errorlevel% equ 0 (
    echo ========================================
    echo   First Run: Generating Credentials
    echo ========================================
    echo Generating unique secure credentials for this installation...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_DIR%generate-secrets.ps1"
    if !errorlevel! neq 0 (
        echo.
        echo ERROR: Failed to generate secure credentials!
        echo.
        echo OpenTranscribe cannot start with placeholder passwords.
        echo Try running this manually from PowerShell:
        echo   powershell -ExecutionPolicy Bypass -File "%INSTALL_DIR%generate-secrets.ps1"
        echo.
        pause
        exit /b 1
    )
    echo.
)

REM Check if docker-images directory exists (first run)
if exist "%INSTALL_DIR%docker-images" (
    echo ========================================
    echo   First Run: Loading Docker Images
    echo ========================================
    echo This is the first time running OpenTranscribe.
    echo Docker images need to be loaded into Docker Desktop.
    echo This will take 5-15 minutes depending on your system.
    echo.

    REM Verify checksums if available
    if exist "%INSTALL_DIR%docker-images\checksums.sha256" (
        echo Verifying file integrity...
        cd "%INSTALL_DIR%docker-images"
        powershell -Command "Get-Content checksums.sha256 | ForEach-Object { $hash, $file = $_ -split '\s+'; if (Test-Path $file) { $computed = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLower(); if ($hash -ne $computed) { Write-Host \"CHECKSUM MISMATCH: $file\" -ForegroundColor Red; exit 1 } else { Write-Host \"OK: $file\" -ForegroundColor Green } } }"
        if %errorlevel% neq 0 (
            echo.
            echo ERROR: File integrity check FAILED!
            echo Docker images may have been corrupted.
            echo Please reinstall OpenTranscribe.
            echo.
            cd "%INSTALL_DIR%"
            pause
            exit /b 1
        )
        cd "%INSTALL_DIR%"
        echo File integrity verified successfully.
        echo.
    )

    REM Load each Docker image
    echo Loading Docker images...
    echo.
    for %%f in ("%INSTALL_DIR%docker-images\*.tar") do (
        echo Loading: %%~nxf
        docker load -i "%%f"
        if %errorlevel% neq 0 (
            echo.
            echo ERROR: Failed to load Docker image %%f
            echo.
            echo Troubleshooting:
            echo   1. Ensure Docker Desktop has enough disk space allocated
            echo   2. Check Docker Desktop settings ^(Resources^)
            echo   3. Try restarting Docker Desktop
            echo.
            pause
            exit /b 1
        )
        echo.
    )

    echo All Docker images loaded successfully.
    echo.

    REM Delete the docker-images directory to save space
    echo Cleaning up image tar files to save disk space...
    rmdir /s /q "%INSTALL_DIR%docker-images"
    echo.
)

REM Detect GPU for docker compose profile selection
echo ========================================
echo   GPU Detection
echo ========================================

set "GPU_AVAILABLE=false"
set "COMPOSE_PROFILES="

REM Check for NVIDIA GPU
if exist "%SystemRoot%\System32\nvidia-smi.exe" (
    nvidia-smi >nul 2>&1
    if !errorlevel! equ 0 (
        echo NVIDIA GPU detected!
        set "GPU_AVAILABLE=true"

        REM Get GPU info
        for /f "delims=" %%i in ('nvidia-smi --query-gpu=name,memory.total --format=csv,noheader') do (
            echo GPU: %%i
        )
        echo.
        echo GPU acceleration will be enabled for transcription.
    ) else (
        echo NVIDIA GPU drivers not properly configured.
        echo Running in CPU mode.
    )
) else (
    echo No NVIDIA GPU detected.
    echo Running in CPU mode ^(transcription will be slower^).
)
echo.

REM Check if services are already running
echo Checking for existing OpenTranscribe containers...
docker compose -f "%INSTALL_DIR%config\docker-compose.yml" -f "%INSTALL_DIR%config\docker-compose.offline.yml" ps --services --filter "status=running" >nul 2>&1
if %errorlevel% equ 0 (
    echo OpenTranscribe services are already running.
    echo.
    echo Opening application in browser...
    timeout /t 2 >nul
    start http://localhost:5173
    echo.
    echo Application is accessible at: http://localhost:5173
    echo.
    echo To stop OpenTranscribe, close this window or run:
    echo   docker compose -f config\docker-compose.yml -f config\docker-compose.offline.yml down
    echo.
    pause
    exit /b 0
)

REM Start Docker Compose services
echo ========================================
echo   Starting OpenTranscribe Services
echo ========================================
echo.

REM Set environment variables for offline mode
REM
REM NOTE (issue #655): this is a FIXED two-file compose chain by design. There is
REM deliberately no docker-compose.diar-native.yml overlay hook here -- the native
REM diar-native sidecar image is never packaged by build-windows-installer.sh, so
REM adding the overlay would just fail to pull it offline. Windows installs are
REM PyAnnote-diarization-only; see INSTALL-WINDOWS.md's HuggingFace token step and
REM README-WINDOWS.md's model-cache section for the operator-facing explanation.
set "COMPOSE_FILE_BASE=%INSTALL_DIR%config\docker-compose.yml"
set "COMPOSE_FILE_OFFLINE=%INSTALL_DIR%config\docker-compose.offline.yml"
set "MODEL_CACHE_DIR=%INSTALL_DIR%models"

echo Starting services with offline configuration...
docker compose -f "%COMPOSE_FILE_BASE%" -f "%COMPOSE_FILE_OFFLINE%" up -d

if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to start OpenTranscribe services!
    echo.
    echo Troubleshooting:
    echo   1. Check Docker Desktop is running
    echo   2. Check logs: docker compose -f config\docker-compose.yml -f config\docker-compose.offline.yml logs
    echo   3. Try: docker compose -f config\docker-compose.yml -f config\docker-compose.offline.yml down
    echo   4. Then run this script again
    echo.
    pause
    exit /b 1
)

echo.
echo Services started successfully.
echo.

REM Wait for services to initialize
echo Waiting for services to initialize...
timeout /t 15 >nul

REM Check service health
echo.
echo Checking service health...
docker compose -f "%COMPOSE_FILE_BASE%" -f "%COMPOSE_FILE_OFFLINE%" ps

echo.
echo ========================================
echo   OpenTranscribe Started Successfully!
echo ========================================
echo.
echo Application is now accessible at:
echo   Frontend:  http://localhost:5173
echo   Backend API: http://localhost:5174
echo   API Docs:    http://localhost:5174/docs
echo.
echo Additional services:
echo   MinIO Console:      http://localhost:5179
echo   Flower ^(Tasks^):     http://localhost:5175
echo   OpenSearch Dashboards: http://localhost:5181
echo.
echo Opening application in browser...
timeout /t 2 >nul
start http://localhost:5173

echo.
echo ========================================
echo   Useful Commands
echo ========================================
echo.
echo View logs:
echo   docker compose -f config\docker-compose.yml -f config\docker-compose.offline.yml logs -f [service]
echo.
echo Stop services:
echo   docker compose -f config\docker-compose.yml -f config\docker-compose.offline.yml down
echo.
echo Restart services:
echo   docker compose -f config\docker-compose.yml -f config\docker-compose.offline.yml restart
echo.
echo Check status:
echo   docker compose -f config\docker-compose.yml -f config\docker-compose.offline.yml ps
echo.
echo For detailed documentation, see README-WINDOWS.md
echo.

REM Keep window open
echo Press any key to close this window...
echo ^(Services will continue running in the background^)
pause >nul

endlocal
