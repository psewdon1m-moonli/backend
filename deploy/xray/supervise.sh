#!/bin/busybox sh
set -eu

busybox=/bin/busybox
xray=/usr/local/bin/xray
fallback=/opt/moonli/direct.json
dynamic=/run/moonli-secrets/xray-routing.json
child=""

stop_child() {
  if [ -n "$child" ] && "$busybox" kill -0 "$child" 2>/dev/null; then
    "$busybox" kill -TERM "$child"
    wait "$child" || true
  fi
}

trap 'stop_child; exit 0' TERM INT

select_config() {
  if [ -s "$dynamic" ] && "$xray" run -test -config "$dynamic" >/dev/null 2>&1; then
    selected="$dynamic"
  else
    selected="$fallback"
  fi
}

fingerprint() {
  "$busybox" sha256sum "$1" | "$busybox" cut -d ' ' -f 1
}

while true; do
  select_config
  active="$selected"
  active_hash="$(fingerprint "$active")"
  "$xray" run -config "$active" &
  child="$!"

  while "$busybox" kill -0 "$child" 2>/dev/null; do
    "$busybox" sleep 1
    select_config
    next_hash="$(fingerprint "$selected")"
    if [ "$selected" != "$active" ] || [ "$next_hash" != "$active_hash" ]; then
      stop_child
      child=""
      break
    fi
  done

  if [ -n "$child" ]; then
    wait "$child" || true
    child=""
    "$busybox" sleep 1
  fi
done
