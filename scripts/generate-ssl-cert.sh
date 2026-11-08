#!/bin/bash
# generate-ssl-cert.sh - Generate a local CA and a server certificate for OpenTranscribe
#
# This script creates SSL certificates for HTTPS access, enabling:
# - Browser microphone recording (required for HTTPS)
# - Secure access from devices on your network
# - Homelab and small business deployments
#
# Usage:
#   ./scripts/generate-ssl-cert.sh [hostname] [--ip IP_ADDRESS]...
#
# Examples:
#   # Simple hostname only
#   ./scripts/generate-ssl-cert.sh opentranscribe.local
#
#   # Hostname + IP addresses (recommended for homelab)
#   ./scripts/generate-ssl-cert.sh opentranscribe.local --ip 192.168.1.100 --ip 10.0.0.50
#
#   # Auto-detect local IP addresses
#   ./scripts/generate-ssl-cert.sh opentranscribe.local --auto-ip
#
# A local certificate authority (nginx/ssl/ca.crt + ca.key) is created once and reused on
# every later run. The server certificate (server.crt) is issued from it, so each device
# trusts ca.crt ONCE and the server certificate can be reissued (new IP, expiry) without
# touching any device again. Only ca.crt ever leaves this machine; ca.key must not.
#
# After generation, install nginx/ssl/ca.crt on client devices.
# See the output instructions or docs-site/docs/installation/small-business-lan.md.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SSL_DIR="$PROJECT_ROOT/nginx/ssl"
CA_DAYS=3650    # the CA is installed on devices by hand, so it is long-lived
# Apple (iOS/macOS) rejects TLS server certificates valid for more than 825 days.
CERT_DAYS=825
KEY_SIZE=2048

# Parse arguments
HOSTNAME=""
IP_ADDRESSES=()
AUTO_IP=false

print_usage() {
    echo "Usage: $0 <hostname> [--ip IP_ADDRESS]... [--auto-ip]"
    echo ""
    echo "Arguments:"
    echo "  hostname      The primary hostname (e.g., opentranscribe.local)"
    echo "  --ip IP       Add an IP address to the certificate (can be used multiple times)"
    echo "  --auto-ip     Automatically detect and add local IP addresses"
    echo ""
    echo "Examples:"
    echo "  $0 opentranscribe.local"
    echo "  $0 opentranscribe.local --ip 192.168.1.100"
    echo "  $0 opentranscribe.local --auto-ip"
    echo "  $0 opentranscribe.local --ip 192.168.1.100 --ip 10.0.0.50"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --ip)
            IP_ADDRESSES+=("$2")
            shift 2
            ;;
        --auto-ip)
            AUTO_IP=true
            shift
            ;;
        --help|-h)
            print_usage
            exit 0
            ;;
        -*)
            echo -e "${RED}Unknown option: $1${NC}"
            print_usage
            exit 1
            ;;
        *)
            if [ -z "$HOSTNAME" ]; then
                HOSTNAME="$1"
            else
                echo -e "${RED}Unexpected argument: $1${NC}"
                print_usage
                exit 1
            fi
            shift
            ;;
    esac
done

# Validate hostname
if [ -z "$HOSTNAME" ]; then
    echo -e "${RED}Error: Hostname is required${NC}"
    print_usage
    exit 1
fi

# The hostname and every --ip value are written into an openssl config file below, so
# anything outside a plain DNS label / dotted quad is rejected before it gets there.
if [[ ${#HOSTNAME} -gt 253 || ! "$HOSTNAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]]; then
    echo -e "${RED}Error: '$HOSTNAME' is not a valid hostname (letters, digits, dots and hyphens only)${NC}"
    exit 1
fi
for ip in ${IP_ADDRESSES[@]+"${IP_ADDRESSES[@]}"}; do
    if [[ ! "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
        echo -e "${RED}Error: '$ip' is not a valid IPv4 address${NC}"
        exit 1
    fi
done

# Auto-detect IP addresses if requested
if [ "$AUTO_IP" = true ]; then
    echo -e "${BLUE}Auto-detecting local IP addresses...${NC}"

    # Get all IPv4 addresses (excluding loopback)
    if command -v ip &> /dev/null; then
        # Linux
        detected_ips=$(ip -4 addr show | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | grep -v '^127\.')
    elif command -v ifconfig &> /dev/null; then
        # macOS/BSD
        detected_ips=$(ifconfig | grep -oE 'inet [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | awk '{print $2}' | grep -v '^127\.')
    else
        echo -e "${YELLOW}Warning: Could not auto-detect IP addresses${NC}"
        detected_ips=""
    fi

    for ip in $detected_ips; do
        echo -e "  Found: ${GREEN}$ip${NC}"
        IP_ADDRESSES+=("$ip")
    done
fi

# Always add localhost and 127.0.0.1
IP_ADDRESSES+=("127.0.0.1")

# Remove duplicates from IP array
mapfile -t IP_ADDRESSES < <(printf '%s\n' "${IP_ADDRESSES[@]}" | sort -u)

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}OpenTranscribe SSL Certificate Generator${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "Hostname: ${GREEN}$HOSTNAME${NC}"
echo -e "IP Addresses: ${GREEN}${IP_ADDRESSES[*]}${NC}"
echo -e "Output directory: ${GREEN}$SSL_DIR${NC}"
echo ""

# Create SSL directory
mkdir -p "$SSL_DIR"

# Build Subject Alternative Names (SAN) extension
SAN_ENTRIES="DNS:$HOSTNAME,DNS:localhost"
for ip in "${IP_ADDRESSES[@]}"; do
    SAN_ENTRIES="$SAN_ENTRIES,IP:$ip"
done

echo -e "${BLUE}Generating SSL certificate...${NC}"
echo -e "Subject Alternative Names: $SAN_ENTRIES"
echo ""

# Umask first: every private key below is created owner-only, not chmod-ed afterwards.
umask 077

CA_KEY="$SSL_DIR/ca.key"
CA_CRT="$SSL_DIR/ca.crt"

# Reuse the existing CA when it is usable (pair matches, not about to expire), so devices that
# already trust it keep working. Otherwise make a new one and say that devices need it again.
ca_usable() {
    [ -f "$CA_KEY" ] && [ -f "$CA_CRT" ] || return 1
    openssl x509 -in "$CA_CRT" -noout -checkend $((60 * 60 * 24 * 30)) >/dev/null 2>&1 || return 1
    local from_cert from_key
    from_cert=$(openssl x509 -in "$CA_CRT" -noout -pubkey 2>/dev/null) || return 1
    from_key=$(openssl pkey -in "$CA_KEY" -pubout 2>/dev/null) || return 1
    [ -n "$from_cert" ] && [ "$from_cert" = "$from_key" ]
}

if ca_usable; then
    echo -e "${GREEN}Reusing the existing local CA: $CA_CRT${NC}"
else
    if [ -f "$CA_CRT" ] || [ -f "$CA_KEY" ]; then
        echo -e "${YELLOW}The existing CA is expired, expiring, or its key does not match.${NC}"
        echo -e "${YELLOW}A new CA is being created: install the new ca.crt on every device.${NC}"
    fi
    echo -e "${BLUE}Creating the local CA...${NC}"
    openssl req -x509 -nodes -newkey rsa:4096 -sha256 -days "$CA_DAYS" \
        -keyout "$CA_KEY" -out "$CA_CRT" \
        -subj "/O=OpenTranscribe/CN=OpenTranscribe Local CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" \
        2>/dev/null
fi
chmod 600 "$CA_KEY"
chmod 644 "$CA_CRT"

# Leaf certificate: request, then sign with the CA using explicit extensions.
LEAF_CSR=$(mktemp)
LEAF_EXT=$(mktemp)
trap 'rm -f "$LEAF_CSR" "$LEAF_EXT"' EXIT
cat > "$LEAF_EXT" << EOF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
subjectAltName = $SAN_ENTRIES
EOF

openssl req -new -nodes -newkey rsa:$KEY_SIZE \
    -keyout "$SSL_DIR/server.key" \
    -out "$LEAF_CSR" \
    -subj "/O=OpenTranscribe/CN=$HOSTNAME" \
    2>/dev/null

# 8 random bytes keep the serial positive and unique without a serial file next to the CA.
openssl x509 -req -sha256 -days "$CERT_DAYS" \
    -in "$LEAF_CSR" \
    -CA "$CA_CRT" -CAkey "$CA_KEY" \
    -set_serial "0x$(openssl rand -hex 8)" \
    -extfile "$LEAF_EXT" \
    -out "$SSL_DIR/server.crt" \
    2>/dev/null

chmod 600 "$SSL_DIR/server.key"
chmod 644 "$SSL_DIR/server.crt"

echo -e "${GREEN}SSL certificate generated successfully!${NC}"
echo ""
echo -e "${BLUE}Files created:${NC}"
echo -e "  CA certificate (install on devices): ${GREEN}$CA_CRT${NC}"
echo -e "  CA private key (keep on this server): ${GREEN}$CA_KEY${NC}"
echo -e "  Server certificate: ${GREEN}$SSL_DIR/server.crt${NC}"
echo -e "  Server private key: ${GREEN}$SSL_DIR/server.key${NC}"
echo ""

echo -e "${BLUE}Certificate Details:${NC}"
openssl x509 -in "$SSL_DIR/server.crt" -noout -subject -dates -ext subjectAltName 2>/dev/null | head -20
echo ""

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}NEXT STEPS - Trust the CA on each device${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${YELLOW}Install $CA_CRT (the CA, not server.crt) on each device that will use${NC}"
echo -e "${YELLOW}OpenTranscribe. Do this once: a renewed server certificate needs nothing more.${NC}"
echo -e "${YELLOW}Never copy ca.key off this server. Anyone holding it can make certificates${NC}"
echo -e "${YELLOW}that your devices will trust.${NC}"
echo ""
echo -e "${GREEN}Start with NGINX${NC}"
echo "  1. Add to your .env file:  NGINX_SERVER_NAME=$HOSTNAME"
echo "  2. ./opentranscribe.sh start"
echo "  3. Access at: https://$HOSTNAME"
echo ""
echo -e "  ${BLUE}Windows:${NC}  double-click ca.crt > Install Certificate > Local Machine >"
echo "            'Trusted Root Certification Authorities'; restart the browser"
echo -e "  ${BLUE}macOS:${NC}    open ca.crt in Keychain Access > double-click it > Trust >"
echo "            'Always Trust'"
echo -e "  ${BLUE}Linux Chrome/Chromium:${NC}  chrome://settings/certificates > Authorities > Import ca.crt"
echo -e "  ${BLUE}Linux Firefox:${NC}  about:preferences#privacy > View Certificates > Authorities > Import ca.crt"
echo -e "  ${BLUE}iOS:${NC}      AirDrop ca.crt > install the profile (Settings > General > VPN & Device"
echo "            Management) > then Settings > General > About > Certificate Trust Settings"
echo "            > enable full trust for 'OpenTranscribe Local CA'"
echo -e "  ${BLUE}Android:${NC}  Settings > Security > Encryption & credentials > Install a certificate >"
echo "            CA certificate > select ca.crt"
echo ""
echo -e "${GREEN}DNS (recommended for homelab)${NC}"
echo "  Add to your router's DNS or /etc/hosts on each device:"
echo "  ${IP_ADDRESSES[0]}  $HOSTNAME"
echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}Certificate generation complete!${NC}"
echo -e "${BLUE}========================================${NC}"
