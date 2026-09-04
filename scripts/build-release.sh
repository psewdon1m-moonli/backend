#!/usr/bin/env bash
set -euo pipefail

version="${1:?semantic version is required}"
image_digest="${2:?image digest is required}"
output="${3:-release-artifacts}"
repository="psewdon1m-moonli/backend"

[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "Release version must be stable semantic version text." >&2
  exit 2
}
[[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "Image digest must be an immutable sha256 digest." >&2
  exit 2
}
updater_source="${UPDATER_SOURCE_DIR:?UPDATER_SOURCE_DIR must point at the pinned updater checkout}"
[[ -x "$updater_source/updater-linux-amd64" ]] || {
  echo "Pinned updater binary is missing." >&2
  exit 3
}
[[ "$($updater_source/updater-linux-amd64 version)" == "0.2.1-moonli.1" ]] || {
  echo "Updater binary does not contain the required Moonli reconciliation revision." >&2
  exit 3
}

root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
output_path="$root/$output"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
rm -rf "$output_path"
mkdir -p "$output_path" "$stage/deploy/nginx" "$stage/updater/systemd"

install -m 0644 "$root/docker-compose.yml" "$stage/docker-compose.yml"
install -m 0644 "$root/compose.production.yml" "$stage/compose.production.yml"
install -m 0755 "$root/deploy/bootstrap.sh" "$stage/deploy/bootstrap.sh"
install -m 0755 "$root/deploy/install.sh" "$stage/deploy/install.sh"
install -m 0644 "$root/deploy/nginx/Dockerfile" "$stage/deploy/nginx/Dockerfile"
install -m 0644 "$root/deploy/nginx/default.conf.template" "$stage/deploy/nginx/default.conf.template"
install -m 0755 "$root/deploy/nginx/10-moonli-certificate.sh" "$stage/deploy/nginx/10-moonli-certificate.sh"
install -m 0755 "$updater_source/install.sh" "$stage/updater/install.sh"
install -m 0755 "$updater_source/updater-linux-amd64" "$stage/updater/updater-linux-amd64"
install -m 0644 "$updater_source/systemd/updater.service" "$stage/updater/systemd/updater.service"
printf '%s\n' \
  "Moonli $version deployment bundle" \
  "Backend: https://github.com/$repository.git" \
  "Updater source: https://github.com/psewdon1m-exocortex/updater.git" \
  "Updater commit: 75eff9607dac4f7dbe9f841bd428f53ee01274a1" \
  "Moonli updater revision: 0.2.1-moonli.1 (deploy/updater-moonli.patch)" \
  > "$stage/RELEASE-CONTENTS.txt"

source_date_epoch="${SOURCE_DATE_EPOCH:-$(date +%s)}"
tar --sort=name --mtime="@$source_date_epoch" --owner=0 --group=0 --numeric-owner \
  -czf "$output_path/moonli-compose.tar.gz" -C "$stage" .
bundle_sha="$(sha256sum "$output_path/moonli-compose.tar.gz" | awk '{print $1}')"
bundle_url="https://github.com/$repository/releases/download/moonli-v$version/moonli-compose.tar.gz"

cat > "$output_path/moonli-release.json" <<EOF
{
  "schema_version": 1,
  "service": "moonli",
  "version": "$version",
  "image": {
    "reference": "ghcr.io/psewdon1m-moonli/backend",
    "digest": "$image_digest"
  },
  "compose_bundle": {
    "url": "$bundle_url",
    "sha256": "$bundle_sha"
  },
  "database_schema": 2,
  "minimum_updater_version": "0.2.1-moonli.1"
}
EOF

(
  cd "$output_path"
  sha256sum moonli-compose.tar.gz > moonli-compose.tar.gz.sha256
  sha256sum moonli-release.json > moonli-release.json.sha256
)
echo "Release artifacts created in $output_path"
