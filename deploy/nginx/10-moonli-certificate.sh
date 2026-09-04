#!/bin/sh
set -eu

certificate_dir=/etc/nginx/certs
certificate_file="$certificate_dir/fullchain.pem"
private_key_file="$certificate_dir/privkey.pem"

if [ -s "$certificate_file" ] && [ -s "$private_key_file" ]; then
    exit 0
fi

if [ "${MOONLI_ENV:-development}" = "production" ]; then
    echo "Production TLS files are missing: fullchain.pem and privkey.pem are required." >&2
    exit 1
fi

domain="${MOONLI_DOMAIN:-localhost}"
mkdir -p "$certificate_dir"
openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
    -keyout "$private_key_file" \
    -out "$certificate_file" \
    -subj "/CN=$domain" \
    -addext "subjectAltName=DNS:$domain,IP:127.0.0.1" >/dev/null 2>&1
chmod 600 "$private_key_file"
chmod 644 "$certificate_file"
