#!/bin/bash
# Shared helpers for the release-test scenarios' generated .env files.
# test-upgrade.sh writes the .env itself and embeds et_service_credentials.

set -euo pipefail

et_rand_hex() {
    # 32 random hex chars
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "${1:-32}"
    else
        python3 -c "import secrets; print(secrets.token_hex($1))" "${1:-32}"
    fi
}

# Production startup checks (startup_validation.py) refuse an unset/default FLOWER_PASSWORD and
# bundled MinIO without KMS auto-encryption, so every rehearsal .env that runs hardened must
# carry both, like a real install. Emits .env lines; the KMS key is generated per run.
et_service_credentials() {
    echo "FLOWER_PASSWORD=$(et_rand_hex 16)"
    echo "MINIO_KMS_SECRET_KEY=opentranscribe-key:$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')"
    echo "MINIO_KMS_AUTO_ENCRYPTION=on"
}
