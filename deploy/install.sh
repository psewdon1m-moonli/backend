#!/usr/bin/env sh
set -eu

install_root="${MOONLI_INSTALL_ROOT:-/opt/moonli}"
env_file="$install_root/.env"
mode="${1:-install}"

fail() {
  echo "Moonli install: $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || fail "run as root"
[ "$(uname -s)" = "Linux" ] || fail "Linux is required"
case "$(uname -m)" in
  x86_64|amd64) ;;
  *) fail "only Linux amd64 is currently supported" ;;
esac

random_hex() {
  od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

prepare() {
  install -d -o root -g root -m 0755 "$install_root"
  if [ -e "$env_file" ]; then
    fail "$env_file already exists; use the updater or an explicit repair"
  fi
  umask 077
  updater_control="$(random_hex)"
  updater_catalog="$(random_hex)"
  cat > "$env_file" <<EOF
# OPERATOR INPUT — edit only this section
MOONLI_DOMAIN=moonli.example.com
MOONLI_ALLOWED_HOSTS=moonli.example.com,127.0.0.1
MOONLI_TLS_CERTS_PATH=/srv/moonli/tls
MOONLI_OPERATOR_ACCESS_KEY=replace-with-a-strong-operator-access-key
MOONLI_CLIENT_API_KEYS=replace-with-one-or-more-independent-client-api-keys
MOONLI_IMAGE_PROVIDER=google
MOONLI_TRANSCRIPTION_PROVIDER=google
MOONLI_NORMALIZATION_PROVIDER=google
MOONLI_GOOGLE_IMAGE_MODEL=replace-with-enabled-google-image-model
MOONLI_GOOGLE_TRANSCRIPTION_MODEL=replace-with-enabled-google-transcription-model
MOONLI_GOOGLE_NORMALIZATION_MODEL=replace-with-enabled-google-text-model
MOONLI_GOOGLE_TRANSLATION_MODEL=gemini-2.5-flash

# GENERATED SECRETS — machine managed
MOONLI_UPDATER_CONTROL_TOKEN=$updater_control
MOONLI_UPDATER_CATALOG_TOKEN=$updater_catalog
KERNEL_URL=http://127.0.0.1:18000
KERNEL_SERVICE_TOKEN=$updater_catalog
UPDATER_SERVICE_ID=moonli
UPDATER_COMPOSE_PROJECT_DIR=/opt/moonli
UPDATER_COMPOSE_FILE=docker-compose.yml
UPDATER_COMPOSE_SERVICE=api
UPDATER_CONTAINER_NAME=moonli-api
UPDATER_IMAGE_VARIABLE=MOONLI_IMAGE
UPDATER_VERSION_VARIABLE=MOONLI_VERSION
UPDATER_LOCAL_HEALTH_URL=http://127.0.0.1:18000/readyz
UPDATER_PUBLIC_HEALTH_URL=https://moonli.example.com/health
UPDATER_RESTORE_URL=http://127.0.0.1:18000/internal/updater/restore
UPDATER_RESTORE_FIELD=file

# RELEASE LOCK — installed from a verified release manifest
MOONLI_IMAGE=replace-with-immutable-image-digest
MOONLI_VERSION=replace-with-release-version

# RUNTIME DEFAULTS
MOONLI_ENV=production
MOONLI_LOOPBACK_API_PORT=18000
MOONLI_HTTP_PORT=80
MOONLI_HTTPS_PORT=443
MOONLI_GOOGLE_API_BASE_URL=https://generativelanguage.googleapis.com/v1beta
MOONLI_GOOGLE_TIMEOUT_SECONDS=180
MOONLI_GOOGLE_IMAGE_ASPECT_RATIO=1:1
MOONLI_GOOGLE_IMAGE_SIZE=1K
MOONLI_MAX_TEXT_LENGTH=12000
MOONLI_MAX_AUDIO_SIZE=20971520
MOONLI_MAX_REQUEST_BODY=22m
MOONLI_REQUESTS_PER_MINUTE=30
MOONLI_CONCURRENT_REQUESTS_PER_CLIENT=2
MOONLI_USAGE_RETENTION_DAYS=365
MOONLI_USAGE_MAX_ROWS=1000000
MOONLI_AUDIT_RETENTION_DAYS=30
MOONLI_AUDIT_MAX_EVENTS=10000
MOONLI_AUDIT_MAX_BYTES=67108864
MOONLI_BACKUP_MAX_COMPRESSED_BYTES=134217728
MOONLI_BACKUP_MAX_UNCOMPRESSED_BYTES=268435456
EOF
  chmod 0600 "$env_file"
  echo "Prepared $env_file"
  echo "Edit only the OPERATOR INPUT section, then run: sudo moonli-admin install"
}

value() {
  sed -n "s/^$1=//p" "$env_file" | tail -n 1
}

validate() {
  [ -f "$env_file" ] || fail "$env_file is missing; run prepare first"
  [ "$(stat -c '%a' "$env_file")" = "600" ] || fail "$env_file must have mode 0600"
  domain="$(value MOONLI_DOMAIN)"
  operator_key="$(value MOONLI_OPERATOR_ACCESS_KEY)"
  client_keys="$(value MOONLI_CLIENT_API_KEYS)"
  image="$(value MOONLI_IMAGE)"
  version="$(value MOONLI_VERSION)"
  tls_dir="$(value MOONLI_TLS_CERTS_PATH)"
  echo "$domain" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9.-]+$' || fail "MOONLI_DOMAIN is invalid"
  [ "${#operator_key}" -ge 24 ] || fail "operator Access Key must be at least 24 characters"
  [ "$operator_key" != "$client_keys" ] || fail "operator and client credentials must differ"
  echo "$operator_key$client_keys$image$version" | grep -q 'replace-with-' && fail "replace every placeholder"
  echo "$image" | grep -Eq '^ghcr\.io/psewdon1m-moonli/backend@sha256:[0-9a-f]{64}$' || fail "MOONLI_IMAGE must be the allow-listed immutable image"
  echo "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$' || fail "MOONLI_VERSION is invalid"
  [ -s "$tls_dir/fullchain.pem" ] && [ -s "$tls_dir/privkey.pem" ] || fail "TLS certificate files are missing"
  if grep -q '^UPDATER_PUBLIC_HEALTH_URL=' "$env_file"; then
    temporary="$env_file.health.tmp"
    sed "s#^UPDATER_PUBLIC_HEALTH_URL=.*#UPDATER_PUBLIC_HEALTH_URL=https://$domain/health#" "$env_file" > "$temporary"
    chmod 0600 "$temporary"
    mv "$temporary" "$env_file"
  fi
  command -v docker >/dev/null 2>&1 || fail "Docker is required"
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
  command -v curl >/dev/null 2>&1 || fail "curl is required"
}

finalize_seed() {
  temporary="$env_file.tmp"
  umask 077
  sed 's/^MOONLI_OPERATOR_ACCESS_KEY=.*/MOONLI_OPERATOR_ACCESS_KEY=/' "$env_file" > "$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$env_file"
}

install_service() {
  validate
  cd "$install_root"
  docker compose --env-file "$env_file" -f docker-compose.yml -f compose.production.yml config >/dev/null
  docker pull "$(value MOONLI_IMAGE)"
  if [ -x "$install_root/updater/install.sh" ] && [ -f "$install_root/updater/updater-linux-amd64" ]; then
    "$install_root/updater/install.sh" moonli "$env_file" "$install_root/updater/updater-linux-amd64"
  else
    fail "verified updater bundle is missing"
  fi
  docker compose --env-file "$env_file" -f docker-compose.yml -f compose.production.yml up -d --build --wait
  domain="$(value MOONLI_DOMAIN)"
  attempts=0
  until curl --fail --silent --show-error --max-time 5 --header "Host: $domain" \
    "http://127.0.0.1:$(value MOONLI_LOOPBACK_API_PORT)/readyz" >/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 12 ]; then
      docker compose --env-file "$env_file" -f docker-compose.yml -f compose.production.yml ps >&2
      docker compose --env-file "$env_file" -f docker-compose.yml -f compose.production.yml logs --tail 80 vless-proxy api gateway >&2
      fail "Moonli loopback readiness check failed"
    fi
    sleep 5
  done
  attempts=0
  until curl --fail --silent --show-error --max-time 5 "https://$domain/health" >/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 24 ]; then
      docker compose --env-file "$env_file" -f docker-compose.yml -f compose.production.yml ps >&2
      docker compose --env-file "$env_file" -f docker-compose.yml -f compose.production.yml logs --tail 80 vless-proxy api gateway >&2
      fail "Moonli did not become healthy"
    fi
    sleep 5
  done
  finalize_seed
  docker compose --env-file "$env_file" -f docker-compose.yml -f compose.production.yml \
    up -d --force-recreate --wait api gateway
  attempts=0
  until curl --fail --silent --show-error --max-time 5 --header "Host: $domain" \
    "http://127.0.0.1:$(value MOONLI_LOOPBACK_API_PORT)/readyz" >/dev/null && \
    curl --fail --silent --show-error --max-time 5 "https://$domain/health" >/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 24 ]; then
      docker compose --env-file "$env_file" -f docker-compose.yml -f compose.production.yml ps >&2
      docker compose --env-file "$env_file" -f docker-compose.yml -f compose.production.yml logs --tail 80 vless-proxy api gateway >&2
      fail "Moonli failed after removing the plaintext operator seed"
    fi
    sleep 5
  done
  echo "Moonli is healthy at https://$domain"
  echo "Set the Google API key from the Production page in the browser."
}

case "$mode" in
  prepare) prepare ;;
  install) install_service ;;
  *) fail "usage: install.sh prepare|install" ;;
esac
