#!/usr/bin/env bash
# Production service-credential preflight, sourced by opentranscribe.sh.
#
# Mirrors backend/app/core/startup_validation.py:validate_service_credentials(), which
# refuses to start a hardened backend when FLOWER_PASSWORD is unset or a known default, or
# when bundled MinIO does not encrypt objects (MINIO_KMS_AUTO_ENCRYPTION not on) without
# ALLOW_UNENCRYPTED_OBJECT_STORE=true. Checking here turns a post-upgrade crash loop into a
# refusal with a remedy while the old stack is still running.
#
# This file only READS .env. It never generates or writes a password or KMS key: losing a
# KMS key makes every object encrypted under it permanently unreadable, so the operator
# adds it deliberately and backs it up.

# Raw `cut` rather than read_env_value: a secret may contain `#`, which read_env_value's
# inline-comment stripping would truncate.
_ot_cred_raw() {
    grep -E "^$1=" "${2:-.env}" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "' || true
}

# Print one problem per line for the .env at $1 (default .env); empty output means OK.
ot_service_credential_problems() {
    local env_file="${1:-.env}" env_name flower storage kms optout
    env_name=$(_ot_cred_raw ENVIRONMENT "$env_file" | tr '[:upper:]' '[:lower:]')
    case "$env_name" in development|dev|testing|test|local) return 0 ;; esac

    flower=$(_ot_cred_raw FLOWER_PASSWORD "$env_file" | tr '[:upper:]' '[:lower:]')
    case "$flower" in
        ""|flower|changeme|*change_me*) echo "FLOWER_PASSWORD is unset or a known default" ;;
    esac

    storage=$(_ot_cred_raw STORAGE_BACKEND "$env_file" | tr '[:upper:]' '[:lower:]')
    kms=$(_ot_cred_raw MINIO_KMS_AUTO_ENCRYPTION "$env_file" | tr '[:upper:]' '[:lower:]')
    optout=$(_ot_cred_raw ALLOW_UNENCRYPTED_OBJECT_STORE "$env_file" | tr '[:upper:]' '[:lower:]')
    if [ -z "$storage" ] || [ "$storage" = "minio" ]; then
        case "$kms" in on|true|1) ;; *)
            case "$optout" in true|1|yes|on) ;; *)
                echo "MINIO_KMS_AUTO_ENCRYPTION is not 'on' (bundled MinIO would store objects unencrypted)" ;;
            esac ;;
        esac
    fi
}

# Run the check; on problems print the remedy and return 1. Prints nothing when OK.
ot_require_service_credentials() {
    local problems
    problems=$(ot_service_credential_problems "${1:-.env}")
    [ -z "$problems" ] && return 0
    echo "❌ Your .env does not satisfy the production startup checks:"
    while IFS= read -r line; do echo "   • $line"; done <<< "$problems"
    echo "   Refusing to continue: the backend would refuse to start. Nothing was changed."
    echo ""
    case "$problems" in *FLOWER_PASSWORD*)
        echo "Add a strong Flower dashboard password to .env:"
        echo "  FLOWER_PASSWORD=<output of: openssl rand -hex 16>" ;;
    esac
    case "$problems" in *MINIO_KMS*)
        echo "Turn on object-store encryption by adding to .env:"
        echo "  MINIO_KMS_SECRET_KEY=opentranscribe-key:<output of: openssl rand -base64 32>"
        echo "  MINIO_KMS_AUTO_ENCRYPTION=on"
        echo "  BACK UP that key before starting. Objects are encrypted with it; if it is lost they"
        echo "  become permanently unreadable. Objects already stored unencrypted stay readable."
        echo "Or, to knowingly store objects unencrypted, add: ALLOW_UNENCRYPTED_OBJECT_STORE=true" ;;
    esac
    return 1
}
