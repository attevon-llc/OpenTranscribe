# ============================================================================
# OpenTranscribe Secret Generation (Windows)
# ============================================================================
#
# Generates cryptographically secure credentials in .env on the FIRST RUN of
# an installed package. Secrets are intentionally NOT baked into the installer
# at build time - that would give every installation of the same package
# identical passwords and encryption keys.
#
# Idempotent: only values still set to the .env.example placeholder
# (CHANGE_ME_auto_generated_on_install) are replaced. Existing real secrets
# are never touched, so re-running (or upgrading) is safe.
#
# Invoked automatically by run_opentranscribe.bat; can also be run manually:
#   powershell -ExecutionPolicy Bypass -File generate-secrets.ps1
# ============================================================================

$ErrorActionPreference = 'Stop'

$EnvFile = Join-Path $PSScriptRoot '.env'
$Placeholder = 'CHANGE_ME_auto_generated_on_install'

if (-not (Test-Path $EnvFile)) {
    Write-Host "ERROR: .env not found at $EnvFile" -ForegroundColor Red
    exit 1
}

# Cryptographically secure random bytes (FIPS-validated CNG provider)
function Get-RandomBytes([int]$Count) {
    $bytes = New-Object byte[] $Count
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return $bytes
}

function Get-RandomHex([int]$ByteCount) {
    return -join ((Get-RandomBytes $ByteCount) | ForEach-Object { $_.ToString('x2') })
}

function Get-RandomBase64([int]$ByteCount) {
    return [Convert]::ToBase64String((Get-RandomBytes $ByteCount))
}

# Key -> generator. Formats match setup-opentranscribe.sh:
# - ENCRYPTION_KEY prefix makes it invalid base64, forcing the backend's
#   PBKDF2 key-derivation path.
# - MINIO_KMS_SECRET_KEY must be <name>:<base64-32-bytes> or MinIO refuses
#   to start (MINIO_KMS_AUTO_ENCRYPTION=on by default).
$Generators = [ordered]@{
    'POSTGRES_PASSWORD'    = { Get-RandomHex 32 }
    'MINIO_ROOT_PASSWORD'  = { Get-RandomHex 32 }
    'JWT_SECRET_KEY'       = { Get-RandomHex 64 }
    'ENCRYPTION_KEY'       = { "opentranscribe_$(Get-RandomBase64 48)" }
    'REDIS_PASSWORD'       = { Get-RandomHex 32 }
    'OPENSEARCH_PASSWORD'  = { Get-RandomHex 32 }
    'FLOWER_PASSWORD'      = { Get-RandomHex 16 }
    'MINIO_KMS_SECRET_KEY' = { "opentranscribe-key:$(Get-RandomBase64 32)" }
}

$lines = [System.IO.File]::ReadAllLines($EnvFile)
$replaced = @()

for ($i = 0; $i -lt $lines.Length; $i++) {
    foreach ($key in $Generators.Keys) {
        if ($lines[$i] -ceq "$key=$Placeholder") {
            $lines[$i] = "$key=$(& $Generators[$key])"
            $replaced += $key
            break
        }
    }
}

if ($replaced.Count -eq 0) {
    Write-Host 'Secure credentials already configured - nothing to do.' -ForegroundColor Green
    exit 0
}

# Write UTF-8 WITHOUT BOM - a BOM corrupts the first variable name when
# docker compose parses the env file.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($EnvFile, $lines, $utf8NoBom)

Write-Host "Generated secure credentials for: $($replaced -join ', ')" -ForegroundColor Green

# Fail loudly if any placeholder survived (e.g. a key this script doesn't know)
$leftover = $lines | Where-Object { $_ -match "=$Placeholder$" }
if ($leftover) {
    Write-Host 'WARNING: unreplaced placeholder values remain in .env:' -ForegroundColor Yellow
    $leftover | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
}

exit 0
