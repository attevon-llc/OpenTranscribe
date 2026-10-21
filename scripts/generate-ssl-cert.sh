#!/bin/bash
# generate-ssl-cert.sh - Generate self-signed SSL certificates for OpenTranscribe
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
# After generation, you'll need to trust the certificate on client devices.
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
CERT_DAYS=3650  # 10 years for self-signed
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

# Create OpenSSL configuration file
OPENSSL_CNF=$(mktemp)
cat > "$OPENSSL_CNF" << EOF
[req]
default_bits = $KEY_SIZE
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
C = US
ST = Local
L = Local
O = OpenTranscribe
OU = Self-Signed
CN = $HOSTNAME

[v3_req]
basicConstraints = critical, CA:TRUE
keyUsage = critical, digitalSignature, keyEncipherment, keyCertSign
extendedKeyUsage = serverAuth
subjectAltName = $SAN_ENTRIES
EOF

# Generate private key and certificate
openssl req -x509 -nodes -days $CERT_DAYS -newkey rsa:$KEY_SIZE \
    -keyout "$SSL_DIR/server.key" \
    -out "$SSL_DIR/server.crt" \
    -config "$OPENSSL_CNF" \
    2>/dev/null

# Clean up temp file
rm -f "$OPENSSL_CNF"

# Set permissions
chmod 600 "$SSL_DIR/server.key"
chmod 644 "$SSL_DIR/server.crt"

echo -e "${GREEN}SSL certificate generated successfully!${NC}"
echo ""
echo -e "${BLUE}Files created:${NC}"
echo -e "  Certificate: ${GREEN}$SSL_DIR/server.crt${NC}"
echo -e "  Private Key: ${GREEN}$SSL_DIR/server.key${NC}"
echo ""

# Show certificate info
echo -e "${BLUE}Certificate Details:${NC}"
openssl x509 -in "$SSL_DIR/server.crt" -noout -subject -dates -ext subjectAltName 2>/dev/null | head -20
echo ""

# Instructions for trusting the certificate
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}NEXT STEPS - Trust the Certificate${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${YELLOW}To avoid browser security warnings, you need to trust this certificate${NC}"
echo -e "${YELLOW}on each device that will access OpenTranscribe.${NC}"
echo ""
echo -e "${GREEN}Option 1: Add to .env and start with NGINX${NC}"
echo "  1. Add to your .env file:"
echo "     NGINX_SERVER_NAME=$HOSTNAME"
echo ""
echo "  2. Start OpenTranscribe:"
echo "     ./opentranscribe.sh start"
echo ""
echo "  3. Access at: https://$HOSTNAME"
echo ""
echo -e "${GREEN}Option 2: Trust certificate on client devices${NC}"
echo ""
echo -e "  ${BLUE}Windows:${NC}"
echo "    1. Double-click $SSL_DIR/server.crt"
echo "    2. Click 'Install Certificate' → 'Local Machine'"
echo "    3. Select 'Place all certificates in: Trusted Root Certification Authorities'"
echo "    4. Complete the wizard and restart browser"
echo ""
echo -e "  ${BLUE}macOS:${NC}"
echo "    1. Double-click $SSL_DIR/server.crt to open in Keychain Access"
echo "    2. Find the certificate, double-click it"
echo "    3. Expand 'Trust' → Set 'When using this certificate' to 'Always Trust'"
echo "    4. Close and enter password to confirm"
echo ""
echo -e "  ${BLUE}Linux (Chrome/Chromium):${NC}"
echo "    1. Go to chrome://settings/certificates"
echo "    2. Click 'Authorities' tab → 'Import'"
echo "    3. Select $SSL_DIR/server.crt"
echo "    4. Check 'Trust this certificate for identifying websites'"
echo ""
echo -e "  ${BLUE}Linux (Firefox):${NC}"
echo "    1. Go to about:preferences#privacy"
echo "    2. Scroll to 'Certificates' → 'View Certificates'"
echo "    3. Click 'Authorities' tab → 'Import'"
echo "    4. Select $SSL_DIR/server.crt and trust for websites"
echo ""
echo -e "  ${BLUE}iOS:${NC}"
echo "    1. Email or AirDrop the .crt file to your device"
echo "    2. Open it → 'Profile Downloaded' appears"
echo "    3. Settings → General → VPN & Device Management → Install profile"
echo "    4. Settings → General → About → Certificate Trust Settings"
echo "    5. Enable trust for the OpenTranscribe certificate"
echo ""
echo -e "  ${BLUE}Android:${NC}"
echo "    1. Copy server.crt to device or download via browser"
echo "    2. Settings → Security → Encryption & credentials"
echo "    3. Install a certificate → CA certificate"
echo "    4. Select the .crt file"
echo ""
echo -e "${GREEN}Option 3: DNS Setup (Recommended for homelab)${NC}"
echo "  Add to your router's DNS or /etc/hosts on each device:"
echo "  ${IP_ADDRESSES[0]}  $HOSTNAME"
echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}Certificate generation complete!${NC}"
echo -e "${BLUE}========================================${NC}"
