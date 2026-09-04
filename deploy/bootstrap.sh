#!/usr/bin/env sh
set -eu

repository="psewdon1m-moonli/backend"
install_root="/opt/moonli"

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
[ "$(uname -s)" = "Linux" ] || { echo "Linux is required." >&2; exit 1; }
[ "$(uname -m)" = "x86_64" ] || { echo "Linux amd64 is required." >&2; exit 1; }
[ ! -e "$install_root/.env" ] || { echo "Moonli is already prepared; use moonli-admin." >&2; exit 1; }

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl jq tar coreutils util-linux
else
  echo "Debian or Ubuntu with apt-get is currently required." >&2
  exit 1
fi

release_json="$(curl --proto '=https' --tlsv1.2 --fail --show-error --location \
  --connect-timeout 10 --max-time 30 --retry 3 \
  "https://api.github.com/repos/$repository/releases?per_page=100")"
tag="$(printf '%s' "$release_json" | jq -r '[.[] | select(.draft == false and .prerelease == false) | .tag_name | select(test("^moonli-v[0-9]+\\.[0-9]+\\.[0-9]+$"))] | sort_by(split("-v")[1] | split(".") | map(tonumber)) | last // empty')"
[ -n "$tag" ] || { echo "No stable Moonli release is available." >&2; exit 1; }
manifest_url="$(printf '%s' "$release_json" | jq -r --arg tag "$tag" '.[] | select(.tag_name == $tag) | .assets[] | select(.name == "moonli-release.json") | .browser_download_url')"
[ -n "$manifest_url" ] || { echo "Release manifest is missing." >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM
curl --proto '=https' --tlsv1.2 --fail --show-error --location --connect-timeout 10 --max-time 60 --retry 3 \
  --max-filesize 2097152 -o "$work/moonli-release.json" "$manifest_url"
jq -e --arg version "${tag#moonli-v}" '
  .schema_version == 1 and .service == "moonli" and .version == $version and
  (.image.reference == "ghcr.io/psewdon1m-moonli/backend") and
  (.image.digest | test("^sha256:[0-9a-f]{64}$")) and
  (.compose_bundle.sha256 | test("^[0-9a-f]{64}$")) and
  (.compose_bundle.url == ("https://github.com/psewdon1m-moonli/backend/releases/download/moonli-v" + $version + "/moonli-compose.tar.gz")) and
  .database_schema == 2 and
  (.minimum_updater_version | test("^[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.-]+)?$"))
' "$work/moonli-release.json" >/dev/null
bundle_url="$(jq -r '.compose_bundle.url' "$work/moonli-release.json")"
bundle_sha="$(jq -r '.compose_bundle.sha256' "$work/moonli-release.json")"
curl --proto '=https' --tlsv1.2 --fail --show-error --location --connect-timeout 10 --max-time 180 --retry 3 \
  --max-filesize 134217728 -o "$work/moonli-compose.tar.gz" "$bundle_url"
printf '%s  %s\n' "$bundle_sha" "$work/moonli-compose.tar.gz" | sha256sum --check --status
tar -tzf "$work/moonli-compose.tar.gz" > "$work/members.txt"
[ "$(wc -l < "$work/members.txt")" -le 32 ] || { echo "Bundle contains too many entries." >&2; exit 1; }
tar -tzf "$work/moonli-compose.tar.gz" | grep -Eq '(^/|(^|/)\.\.(/|$))' && { echo "Unsafe bundle path." >&2; exit 1; }
tar -tvzf "$work/moonli-compose.tar.gz" | grep -Eq '^[lh]' && { echo "Bundle links are forbidden." >&2; exit 1; }
duplicates="$(sed 's#^\./##; /\/$/d' "$work/members.txt" | sort | uniq -d)"
[ -z "$duplicates" ] || { echo "Duplicate bundle paths are forbidden." >&2; exit 1; }
sed 's#^\./##; /\/$/d' "$work/members.txt" | while IFS= read -r member; do
  case "$member" in
    docker-compose.yml|compose.production.yml|RELEASE-CONTENTS.txt|deploy/bootstrap.sh|deploy/install.sh|deploy/nginx/Dockerfile|deploy/nginx/default.conf.template|deploy/nginx/10-moonli-certificate.sh|deploy/xray/Dockerfile|deploy/xray/direct.json|deploy/xray/supervise.sh|updater/install.sh|updater/updater-linux-amd64|updater/systemd/updater.service) ;;
    *) echo "Unexpected bundle member: $member" >&2; exit 1 ;;
  esac
done
install -d -o root -g root -m 0755 "$install_root"
tar --extract --gzip --file "$work/moonli-compose.tar.gz" --directory "$install_root" --no-same-owner --no-same-permissions
chmod 0755 "$install_root/deploy/install.sh"
chmod 0755 "$install_root/deploy/xray/supervise.sh"
chmod 0644 "$install_root/deploy/xray/Dockerfile" "$install_root/deploy/xray/direct.json"
chmod 0755 "$install_root/updater/install.sh" "$install_root/updater/updater-linux-amd64"
chmod 0644 "$install_root/docker-compose.yml" "$install_root/compose.production.yml" "$install_root/updater/systemd/updater.service"
ln -sf "$install_root/deploy/install.sh" /usr/local/sbin/moonli-admin
"$install_root/deploy/install.sh" prepare
